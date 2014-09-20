# Copyright 2010 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import httplib

import six.moves.urllib.parse as urlparse

from glance.common import exception
from glance.openstack.common import jsonutils
import glance.openstack.common.log as logging
import glance.store.base
import glance.store.location

LOG = logging.getLogger(__name__)


MAX_REDIRECTS = 5


class StoreLocation(glance.store.location.StoreLocation):

    """Class describing an HTTP(S) URI"""

    def process_specs(self):
        self.scheme = self.specs.get('scheme', 'http')
        self.netloc = self.specs['netloc']
        self.user = self.specs.get('user')
        self.password = self.specs.get('password')
        self.path = self.specs.get('path')

    def _get_credstring(self):
        if self.user:
            return '%s:%s@' % (self.user, self.password)
        return ''

    def get_uri(self):
        return "%s://%s%s%s" % (
            self.scheme,
            self._get_credstring(),
            self.netloc,
            self.path)

    def parse_uri(self, uri):
        """
        Parse URLs. This method fixes an issue where credentials specified
        in the URL are interpreted differently in Python 2.6.1+ than prior
        versions of Python.
        """
        pieces = urlparse.urlparse(uri)
        assert pieces.scheme in ('https', 'http')
        self.scheme = pieces.scheme
        netloc = pieces.netloc
        path = pieces.path
        try:
            if '@' in netloc:
                creds, netloc = netloc.split('@')
            else:
                creds = None
        except ValueError:
            # Python 2.6.1 compat
            # see lp659445 and Python issue7904
            if '@' in path:
                creds, path = path.split('@')
            else:
                creds = None
        if creds:
            try:
                self.user, self.password = creds.split(':')
            except ValueError:
                reason = (_("Credentials '%s' not well-formatted.")
                          % "".join(creds))
                LOG.debug(reason)
                raise exception.BadStoreUri()
        else:
            self.user = None
        if netloc == '':
            reason = _("No address specified in HTTP URL")
            LOG.debug(reason)
            raise exception.BadStoreUri(message=reason)
        self.netloc = netloc
        self.path = path

        self.token = None
        if pieces.query:
            params = pieces.query.split('&')
            for param in params:
                if 'auth_token' == param.split("=")[0].strip():
                    self.token = param.split("=")[1]
                    break


def http_response_iterator(conn, response, size):
    """
    Return an iterator for a file-like object.

    :param conn: HTTP(S) Connection
    :param response: httplib.HTTPResponse object
    :param size: Chunk size to iterate with
    """
    chunk = response.read(size)
    while chunk:
        yield chunk
        chunk = response.read(size)
    conn.close()


class Store(glance.store.base.Store):

    """An implementation of the HTTP(S) Backend Adapter"""

    def get(self, location):
        """
        Takes a `glance.store.location.Location` object that indicates
        where to find the image file, and returns a tuple of generator
        (for reading the image file) and image_size

        :param location `glance.store.location.Location` object, supplied
                        from glance.store.location.get_location_from_uri()
        """
        conn, resp, content_length = self._query(location, 'GET')

        iterator = http_response_iterator(conn, resp, self.CHUNKSIZE)

        class ResponseIndexable(glance.store.Indexable):

            def another(self):
                try:
                    return self.wrapped.next()
                except StopIteration:
                    return ''

        return (ResponseIndexable(iterator, content_length), content_length)

    def get_schemes(self):
        return ('http', 'https')

    def get_size(self, location):
        """
        Takes a `glance.store.location.Location` object that indicates
        where to find the image file, and returns the size

        :param location `glance.store.location.Location` object, supplied
                        from glance.store.location.get_location_from_uri()
        """
        try:
            return self._query(location, 'HEAD')[2]
        except Exception:
            return 0

    def _query(self, location, verb, depth=0):
        if depth > MAX_REDIRECTS:
            reason = (_("The HTTP URL exceeded %s maximum "
                        "redirects.") % MAX_REDIRECTS)
            LOG.debug(reason)
            raise exception.MaxRedirectsExceeded(redirects=MAX_REDIRECTS)
        loc = location.store_location
        conn_class = self._get_conn_class(loc)
        conn = conn_class(loc.netloc)
        hearders = {}
        if loc.token:
            hearders.setdefault('x-auth-token', loc.token)
            verb = 'GET'
            conn.request(verb, loc.path, "", hearders)
            resp = conn.getresponse()
            try:
                size = jsonutils.loads(resp.read())['size']
            except Exception:
                size = 0
                raise exception.BadStoreUri(loc.path, reason)
            return (conn, resp, size)

        conn.request(verb, loc.path, "", hearders)
        resp = conn.getresponse()

        # Check for bad status codes
        if resp.status >= 400:
            reason = _("HTTP URL returned a %s status code.") % resp.status
            LOG.debug(reason)
            raise exception.BadStoreUri(loc.path, reason)

        location_header = resp.getheader("location")
        if location_header:
            if resp.status not in (301, 302):
                reason = (_("The HTTP URL attempted to redirect with an "
                            "invalid %s status code.") % resp.status)
                LOG.debug(reason)
                raise exception.BadStoreUri(loc.path, reason)
            location_class = glance.store.location.Location
            new_loc = location_class(location.store_name,
                                     location.store_location.__class__,
                                     uri=location_header,
                                     image_id=location.image_id,
                                     store_specs=location.store_specs)
            return self._query(new_loc, verb, depth + 1)
        content_length = int(resp.getheader('content-length', 0))
        return (conn, resp, content_length)

    def _get_conn_class(self, loc):
        """
        Returns connection class for accessing the resource. Useful
        for dependency injection and stubouts in testing...
        """
        return {'http': httplib.HTTPConnection,
                'https': httplib.HTTPSConnection}[loc.scheme]

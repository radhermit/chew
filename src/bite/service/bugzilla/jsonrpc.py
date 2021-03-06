"""Support Bugzilla's deprecated JSON-RPC interface."""

try: import simplejson as json
except ImportError: import json

from ._rpc import Bugzilla4_4Rpc, Bugzilla5_0Rpc, Bugzilla5_2Rpc, _SearchRequest5_0
from .._jsonrpc import Jsonrpc


class _BugzillaJsonrpcBase(Jsonrpc):
    """Base service class for Bugzilla JSON-RPC interface."""

    def __init__(self, **kw):
        super().__init__(endpoint='/jsonrpc.cgi', **kw)


class Bugzilla4_4Jsonrpc(_BugzillaJsonrpcBase, Bugzilla4_4Rpc):
    """Service for Bugzilla 4.4 JSON-RPC interface.

    API docs: https://www.bugzilla.org/docs/4.4/en/html/api/Bugzilla/WebService/Server/JSONRPC.html
    """
    _service = 'bugzilla4.4-jsonrpc'


# TODO: notify upstream that API docs link returns 404
class Bugzilla5_0Jsonrpc(_BugzillaJsonrpcBase, Bugzilla5_0Rpc):
    """Service for Bugzilla 5.0 JSON-RPC interface."""

    _service = 'bugzilla5.0-jsonrpc'


# TODO: notify upstream that API docs link returns 404
class BugzillaJsonrpc(_BugzillaJsonrpcBase, Bugzilla5_2Rpc):
    """Service for Bugzilla 5.2 JSON-RPC interface."""

    _service = 'bugzilla5.2-jsonrpc'


# TODO: note that this currently isn't being kept up to date
class _StreamingBugzillaJsonrpc(BugzillaJsonrpc):

    _service = None

    def parse_response(self, response):
        return self._IterContent(response)

    class SearchRequest(_SearchRequest5_0):

        def __init__(self, **kw):
            """Construct a search request."""
            super().__init__(**kw)

        def parse(self, data):
            import ijson.backends.yajl2 as ijson
            bugs = ijson.items(data, 'result.bugs.item')
            return (self.service.item(service=self.service, bug=bug) for bug in bugs)

    class _IterContent(object):

        def __init__(self, file, size=64*1024):
            self.initial = True
            self.chunks = file.iter_content(chunk_size=size)

        def read(self, size=64*1024):
            chunk = next(self.chunks)
            # check the initial chunk for errors
            if self.initial:
                self.initial = False
                try:
                    error = json.loads(chunk)['error']
                except json.decoder.JSONDecodeError as e:
                    # if we can't load it, assume it's a valid json doc chunk
                    return chunk
                if error is not None:
                    super().handle_error(error)
            return chunk

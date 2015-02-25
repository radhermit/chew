from urllib.parse import urlparse, urlunparse
from xmlrpclib import ProtocolError, ServerProxy, Transport, Fault, GzipDecodedResponse
#from xml.sax.xmlreader import IncrementalParser

import requests

from bite.objects import decompress
from bite.exceptions import RequestError, AuthError
from bite.services.bugzilla import Bugzilla, BugzillaAttachment

class BugzillaXmlrpc(Bugzilla):
    def __init__(self, **kw):
        url = urlparse(kw['base'])
        path = url.path.rpartition('/')[0]
        url = (url.scheme, url.netloc, path + '/xmlrpc.cgi', None, None, None)
        kw['base'] = urlunparse(url)
        self.headers = {'Content-Type': 'text/xml'}
        super(BugzillaXmlrpc, self).__init__(**kw)
        self.xmlrpc = BugzillaProxy(service=self, uri=self.base)
        self.attachment = BugzillaAttachmentXml

    def create_request(self, method, params=None):
        """Construct and return a tuple containing the XMLRPC method and params to send."""
        return (getattr(self.xmlrpc, method), params)

    @staticmethod
    def _request(request):
        """Send request object and perform checks on the response."""
        cmd, params = request
        try:
            return cmd(params)
        except Fault as e:
            # Fault code 410 means login required
            if e.faultCode == 410:
                raise AuthError(msg=e.faultString, code=e.faultCode)
            else:
                raise RequestError(msg=e.faultString, code=e.faultCode)

class BugzillaProxy(ServerProxy):
    def __init__(self, service, uri, verbose=0, allow_none=0, use_datetime=1,):

        transport = RequestTransport(service, use_datetime=use_datetime, uri=uri)
        ServerProxy.__init__(self, uri=uri, transport=transport,
                verbose=verbose, allow_none=allow_none, use_datetime=use_datetime)

class RequestTransport(Transport):
    def __init__(self, service, uri, use_datetime=1):
        self.service = service
        self.uri = uri
        Transport.__init__(self, use_datetime=use_datetime)

    def request(self, host, handler, request_body, verbose=0):
        try:
            r = self.service.session.post(self.uri, data=request_body, cookies=self.service.auth_token,
                                          headers=self.service.headers, verify=self.service.verify, stream=True,
                                          timeout=self.service.timeout)
        except:
            raise

        if r.status_code == requests.codes.ok:
            return self.parse_response(IterContent(r))
        else:
            raise ProtocolError(self.uri, resp.status, resp.reason, resp.msg)

    def parse_response(self, response):
        # read response data from httpresponse, and parse it
        stream = response

        p, u = self.getparser()

        while 1:
            data = stream.read(64*1024)
            if not data:
                break
            p.feed(data)

        if stream is not response:
            stream.close()
        p.close()

        return u.close()

class IterContent(object):
    def __init__(self, file, size=64*1024):
        self.initial = True
        self.chunks = file.iter_content(chunk_size=size)

    def read(self, size=64*1024):
        try:
            return next(self.chunks)
        except StopIteration:
            return

class BugzillaAttachmentXml(BugzillaAttachment):
    @decompress
    def read(self):
        return self.data.data

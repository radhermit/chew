from xml.parsers.expat import ExpatError

from lxml.etree import XMLPullParser, XMLSyntaxError
from snakeoil.klass import steal_docs

from . import Service
from ..exceptions import ParsingError, RequestError


class Xml(Service):
    """Support generic services that use XML to communicate."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.session.headers.update({
            'Accept': 'text/xml',
            'Content-Type': 'text/xml'
        })

    @steal_docs(Service)
    def parse_response(self, response):
        try:
            return self._parse_xml(response)[0]
        except (ExpatError, XMLSyntaxError) as e:
            # The default XML parser in python (expat) has issues with badly
            # formed XML. We workaround this somewhat by using lxml for parsing
            # which allows recovering from certain types of broken XML.
            if not response.headers['Content-Type'].startswith('text/xml'):
                msg = 'non-XML response from server'
                if not self.verbose:
                    msg += ' (use verbose mode to see it)'
                raise RequestError(msg=msg, text=response.text)
            raise ParsingError(msg='failed parsing XML', text=str(e)) from e

    def _getparser(self, unmarshaller=None):
        u = unmarshaller
        if u is None:
            u = UnmarshallToDict()
        p = LXMLParser(u)
        return p, u

    def _parse_xml(self, response):
        """Parse XML data from response."""
        stream = _IterContent(response)

        p, u = self._getparser()

        while 1:
            data = stream.read(64*1024)
            if not data:
                break
            p.feed(data)
        p.close()

        return u.close()

    def dumps(self, s):
        """Encode dictionary object to XML."""
        raise NotImplementedError

    def loads(self, s):
        """Decode XML to dictionary object."""
        raise NotImplementedError


class _IterContent(object):

    def __init__(self, file, size=64*1024):
        self.initial = True
        self.chunks = file.iter_content(chunk_size=size)

    def read(self, size=64*1024):
        try:
            return next(self.chunks)
        except StopIteration:
            return b''


class LXMLParser(object):
    """XML parser using lxml.

    That tries hard to parse through broken XML.
    """

    def __init__(self, target):
        self._parser = XMLPullParser(events=('start', 'end'), recover=True)
        self._target = target

    def handle_events(self):
        for action, element in self._parser.read_events():
            if action == 'start':
                self._target.start(element.tag, element.attrib)
            elif action == 'end':
                if element.text:
                    self._target.data(element.text)
                self._target.end(element.tag)
                element.clear()

    def feed(self, data):
        try:
            self._parser.feed(data)
        except:
            raise
        self.handle_events()

    def close(self):
        self._parser.close()


class UnmarshallToDict(object):
    pass

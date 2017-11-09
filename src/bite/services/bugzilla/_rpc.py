import base64
import codecs
from itertools import groupby
import os

from . import (Bugzilla, BugzillaBug, BugzillaComment, BugzillaEvent, parsetime,
    ExtensionsRequest, VersionRequest, FieldsRequest, UsersRequest)
from .. import Request, RPCRequest, NullRequest, command, request
from ... import const, magic
from ...exceptions import BiteError


class BugzillaRpc(Bugzilla):
    pass


@request(BugzillaRpc)
class _LoginRequest(RPCRequest):
    def __init__(self, user, password, service, restrict_login=None):
        """Log in as a user and get an auth token."""
        if restrict_login is None:
            restrict_login = service.restrict_login

        params = {
            'login': user,
            'password': password,
            'restrict_login': restrict_login,
        }
        super().__init__(service=service, command='User.login', params=params)

    def parse(self, data):
        return next(data)['token']


@command('users', BugzillaRpc)
@request(BugzillaRpc)
class _UsersRequest(UsersRequest, RPCRequest):
    def __init__(self, *args, **kw):
        super().__init__(command='User.get', *args, **kw)


@command('products', BugzillaRpc)
@request(BugzillaRpc)
class _ProductsRequest(RPCRequest):
    def __init__(self, service, ids=None, names=None, match=None):
        """Query bugzilla for product data."""
        if not any((ids, names)):
            raise ValueError('No user ID(s) or name(s) specified')

        params = {}

        if ids is not None:
            params['ids'] = ids
        if names is not None:
            params['names'] = names

        super().__init__(service=service, command='Product.get', params=params)

    def parse(self, data):
        return next(data)['products']


@command('extensions', BugzillaRpc)
@request(BugzillaRpc)
class _ExtensionsRequest(ExtensionsRequest, RPCRequest):
    def __init__(self, service):
        """Construct an extensions request."""
        super().__init__(service=service, command='Bugzilla.extensions')


@command('version', BugzillaRpc)
@request(BugzillaRpc)
class _VersionRequest(VersionRequest, RPCRequest):
    def __init__(self, service):
        """Construct a version request."""
        super().__init__(service=service, command='Bugzilla.version')


@command('get', BugzillaRpc)
@request(BugzillaRpc)
class _GetRequest(Request):
    def __init__(self, ids, service, get_comments=False, get_attachments=False,
                 get_history=False, *args, **kw):
        """Construct requests to retrieve all known data for given bug IDs."""
        if not ids:
            raise ValueError('No bug ID(s) specified')

        reqs = [service.GetItemRequest(ids=ids)]
        for call in ('attachments', 'comments', 'history'):
            if locals()['get_' + call]:
                reqs.append(getattr(service, call.capitalize() + 'Request')(ids=ids))
            else:
                reqs.append(NullRequest(service=service, generator=True))

        super().__init__(service=service, reqs=reqs)

    def parse(self, data):
        bugs, attachments, comments, history = data
        return (self.service.item(self.service, bug, next(comments),
                                  next(attachments), next(history))
                for bug in bugs)


@request(BugzillaRpc)
class _GetItemRequest(RPCRequest):
    def __init__(self, ids, service, fields=None, **kw):
        """Construct a get request."""
        if not ids:
            raise ValueError('No bug ID(s) specified')

        params = {}
        params['permissive'] = True
        params['ids'] = ids
        if fields is not None:
            params['include_fields'] = fields

        super().__init__(service=service, command='Bug.get', params=params)

    def parse(self, data):
        return next(data)['bugs']


@command('create', BugzillaRpc)
@request(BugzillaRpc)
class _CreateRequest(RPCRequest):
    def __init__(self, service, product, component, version, summary, description=None, op_sys=None,
                 platform=None, priority=None, severity=None, alias=None, assigned_to=None,
                 cc=None, target_milestone=None, groups=None, status=None, **kw):
        """Create a new bug given a list of parameters

        :returns: ID of the newly created bug
        :rtype: int
        """
        params = {}
        params['product'] = product
        params['component'] = component
        params['version'] = version
        params['summary'] = summary
        if description is not None:
            params['description'] = description
        if op_sys is not None:
            params['op_sys'] = op_sys
        if platform is not None:
            params['platform'] = platform
        if priority is not None:
            params['priority'] = priority
        if severity is not None:
            params['severity'] = severity
        if alias is not None:
            params['alias'] = alias
        if assigned_to is not None:
            params['assigned_to'] = assigned_to
        if cc is not None:
            params['cc'] = cc
        if target_milestone is not None:
            params['target_milestone'] = target_milestone
        if groups is not None:
            params['groups'] = groups
        if status is not None:
            params['status'] = status

        super().__init__(service=service, command='Bug.create', params=params)

    def parse(self, data, *args, **kw):
        return next(data)['id']


@command('search', BugzillaRpc)
@request(BugzillaRpc)
class _SearchRequest(RPCRequest):
    def __init__(self, service, *args, **kw):
        """Construct a search request."""
        params = {}
        options_log = []
        for k, v in ((k, v) for (k, v) in kw.items() if v):
            if k in BugzillaBug.attributes:
                if k in ['creation_time', 'last_change_time']:
                    params[k] = v[1]
                    options_log.append('{}: {} (since {} UTC)'.format(
                        BugzillaBug.attributes[k], v[0], parsetime(v[1])))
                elif k in ['assigned_to', 'creator']:
                    params[k] = list(map(service._resuffix, v))
                    options_log.append('{}: {}'.format(
                        BugzillaBug.attributes[k], ', '.join(map(str, v))))
                elif k == 'status':
                    status_alias = []
                    status_map = {
                        'open': service.cache['open_status'],
                        'closed': service.cache['closed_status'],
                        'all': service.cache['open_status'] + service.cache['closed_status'],
                    }
                    for status in v:
                        if status_map.get(status.lower(), False):
                            status_alias.append(status)
                            params.setdefault(k, []).extend(status_map[status.lower()])
                        else:
                            params.setdefault(k, []).append(status)
                    if status_alias:
                        options_log.append('{}: {} ({})'.format(
                            BugzillaBug.attributes[k], ', '.join(status_alias), ', '.join(params[k])))
                    else:
                        options_log.append('{}: {}'.format(
                            BugzillaBug.attributes[k], ', '.join(params[k])))
                else:
                    params[k] = v
                    options_log.append('{}: {}'.format(
                        BugzillaBug.attributes[k], ', '.join(map(str, v))))
            else:
                if k == 'terms':
                    params['summary'] = v
                    options_log.append('{}: {}'.format('Summary', ', '.join(map(str, v))))
                elif k == 'commenter':
                    # XXX: probably fragile since it uses custom search URL params
                    # only works with >=bugzilla-5, previous versions return invalid parameter errors
                    for i, val in enumerate(v):
                        i = str(i + 1)
                        params['f' + i] = 'commenter'
                        params['o' + i] = 'substring'
                        params['v' + i] = val
                    options_log.append('{}: {}'.format('Commenter', ', '.join(map(str, v))))
                elif k in ['limit', 'offset', 'votes']:
                    params[k] = v

        if not params:
            raise BiteError('no supported search terms or options specified')

        # only return open bugs by default
        if not 'status' in params:
            params['status'] = service.cache['open_status']

        if kw.get('fields', None) is None:
            fields = ['id', 'assigned_to', 'summary']
        else:
            fields = kw['fields']
            unknown_fields = set(fields).difference(BugzillaBug.attributes.keys())
            if unknown_fields:
                raise BiteError('unknown fields: {}'.format(', '.join(unknown_fields)))
            options_log.append('{}: {}'.format('Fields', ' '.join(fields)))

        params['include_fields'] = fields

        super().__init__(service=service, command='Bug.search', params=params)
        self.fields = fields
        self.options = options_log

    def parse(self, data, *args, **kw):
        bugs = next(data)['bugs']
        return (self.service.item(service=self.service, bug=bug) for bug in bugs)


@command('comments', BugzillaRpc)
@request(BugzillaRpc)
class _CommentsRequest(RPCRequest):
    def __init__(self, ids=None, comment_ids=None, created=None, fields=None, service=None, **kw):
        """Construct a comments request."""
        if ids is None and comment_ids is None:
            raise ValueError('No {} or comment ID(s) specified'.format(self.service.item_name))

        params = {}
        options_log = []

        if ids is not None:
            ids = list(map(str, ids))
            params['ids'] = ids
            options_log.append('IDs: {}'.format(', '.join(ids)))
        if comment_ids is not None:
            comment_ids = list(map(str, comment_ids))
            params['comment_ids'] = comment_ids
            options_log.append('Comment IDs: {}'.format(', '.join(comment_ids)))
        if created is not None:
            params['new_since'] = created
            options_log.append('Created: {} (since {} UTC)'.format(
                created[0], parsetime(created[1])))
        if fields is not None:
            params['include_fields'] = fields

        self.ids = ids

        super().__init__(service=service, command='Bug.comments', params=params)
        self.options = options_log

    def parse(self, data, *args, **kw):
        bugs = next(data)['bugs']
        for i in self.ids:
            yield [BugzillaComment(comment=comment, id=i, count=j)
                   for j, comment in enumerate(bugs[str(i)]['comments'])]


class ChangesRequest(RPCRequest):
    pass


@command('modify', BugzillaRpc)
@request(BugzillaRpc)
class _ModifyRequest(RPCRequest):
    def __init__(self, service, ids, *args, **kw):
        """Construct a modify request."""
        options_log = []
        params = {}
        for k, v in ((k, v) for (k, v) in kw.items() if v):
            if k in BugzillaBug.attributes:
                if k == 'assigned_to':
                    v = self.service._resuffix(v)
                params[k] = v
                options_log.append('{:<10}: {}'.format(BugzillaBug.attributes[k], v))
            elif '-' in k:
                keys = k.split('-')
                if len(keys) != 2:
                    raise RuntimeError('Argument parsing error')
                else:
                    if keys[0] == 'cc':
                        v = list(map(self.service._resuffix, v))
                    if k == 'comment-body':
                        v = codecs.getdecoder('unicode_escape')(v)[0]

                    if keys[0] not in kw:
                        params[keys[0]] = {}

                    params[keys[0]][keys[1]] = v

                    if keys[1] in ['add', 'remove', 'set']:
                        options_log.append((keys[0], keys[1], v))
                    elif keys[0] == 'comment':
                        pass
                    else:
                        try:
                            options_log.append('{:<10}: {}'.format(BugzillaBug.attributes[keys[0]], v))
                        except KeyError:
                            options_log.append('{:<10}: {}'.format(keys[0].capitalize(), v))
            else:
                if k == 'fixed':
                    params['status'] = 'RESOLVED'
                    params['resolution'] = 'FIXED'
                    options_log.append('Status    : RESOLVED')
                    options_log.append('Resolution: FIXED')
                elif k == 'invalid':
                    params['status'] = 'RESOLVED'
                    params['resolution'] = 'INVALID'
                    options_log.append('Status    : RESOLVED')
                    options_log.append('Resolution: INVALID')

        merge_targets = ((i, x) for i, x in enumerate(options_log) if isinstance(x, tuple))
        merge_targets = sorted(merge_targets, key=lambda x: x[1][0])
        old_indices = [i for (i, x) in merge_targets]
        for key, group in groupby(merge_targets, lambda x: x[1][0]):
            value = []
            for (_, (key, action, values)) in group:
                if action == 'add':
                    value.extend(['+' + str(x) for x in values])
                elif action == 'remove':
                    value.extend(['-' + str(x) for x in values])
                elif action == 'set':
                    value = values
                    break
            try:
                options_log.append('{:<10}: {}'.format(BugzillaBug.attributes[key], ', '.join(value)))
            except KeyError:
                options_log.append('{:<10}: {}'.format(key.capitalize(), ', '.join(value)))

        # remove old entries
        options_log = [x for i, x in enumerate(options_log) if i not in old_indices]

        if not params:
            raise ValueError('No changes specified')

        if options_log:
            prefix = '--- Modifying fields '
            options_log.insert(0, prefix + '-' * (const.COLUMNS - len(prefix)))

        if 'comment' in params:
            prefix = '--- Adding comment '
            options_log.append(prefix + '-' * (const.COLUMNS - len(prefix)))
            options_log.append(params['comment']['body'])

        options_log.append('-' * const.COLUMNS)

        if not ids:
            raise ValueError('No bug ID(s) specified')
        params['ids'] = ids

        super().__init__(service=service, command='Bug.update', params=params)
        self.options = options_log

    def parse(self, data, *args, **kw):
        return next(data)['bugs']


@command('attach', BugzillaRpc)
@request(BugzillaRpc)
class _AttachRequest(RPCRequest):
    def __init__(self, ids, data=None, filepath=None, filename=None, mimetype=None,
                 is_patch=False, is_private=False, comment=None, summary=None, **kw):
        """Add an attachment to a bug

        :param ids: The ids or aliases of bugs that you want to add the attachment to.
        :type ids: list of ints and/or strings
        :param data: Raw attachment data
        :type data: binary data
        :param filepath: Path to the file.
        :type filepath: string
        :param filename: The file name that will be displayed in the UI for the attachment.
        :type filename: string
        :param mimetype: The MIME type of the attachment, like text/plain or image/png.
        :type mimetype: string
        :param comment: A comment to add along with the attachment.
        :type comment: string
        :param summary: A short string describing the attachment.
        :type summary: string
        :param is_patch: True if Bugzilla should treat this attachment as a patch.
            If specified, a content_type doesn't need to be specified as it is forced to text/plain.
            Defaults to false if unspecified.
        :type is_patch: boolean
        :param is_private: True if the attachment should be private, False if public.
            Defaults to false if unspecified.
        :type is_private: boolean

        :raises ValueError: if no bug IDs are specified
        :raises ValueError: if data or filepath arguments aren't specified
        :raises ValueError: if data isn't defined and filepath points to a nonexistent file
        :raises ValueError: if filepath isn't defined and summary or filename isn't specified

        :returns: attachment IDs created
        :rtype: list of attachment IDs
        """
        if not ids:
            raise ValueError('No bug ID(s) or aliases specified')

        params = {'ids': ids}

        if data is not None:
            params['data'] = base64.b64encode(data)
        else:
            if filepath is None:
                raise ValueError('Either data or a filepath must be passed as an argument')
            else:
                if not os.path.exists(filepath):
                    raise ValueError('File not found: {}'.format(filepath))
                else:
                    with open(filepath, 'rb') as f:
                        params['data'] = base64.b64encode(f.read())

        if filename is None:
            if filepath is not None:
                filename = os.path.basename(filepath)
            else:
                raise ValueError('A valid filename must be specified')

        if mimetype is None and not is_patch:
            if data is not None:
                mimetype = magic.from_buffer(data, mime=True)
            else:
                mimetype = magic.from_file(filepath, mime=True)

        if summary is None:
            if filepath is not None:
                summary = filename
            else:
                raise ValueError('A valid summary must be specified')

        params['file_name'] = filename
        params['summary'] = summary
        if not is_patch:
            params['content_type'] = mimetype
        params['comment'] = comment
        params['is_patch'] = is_patch

        super().__init__(service=service, command='Bug.add_attachment', params=params)

    def parse(self, data, *args, **kw):
        return next(data)['attachments']


@command('attachments', BugzillaRpc)
@request(BugzillaRpc)
class _AttachmentsRequest(RPCRequest):
    def __init__(self, service, ids=None, attachment_ids=None, fields=None,
                 get_data=False, *args, **kw):
        """Construct a attachments request."""
        if ids is None and attachment_ids is None:
            raise ValueError('No {} or attachment ID(s) specified'.format(self.service.item_name))

        params = {}
        options_log = []

        if ids is not None:
            ids = list(map(str, ids))
            params['ids'] = ids
            options_log.append('IDs: {}'.format(', '.join(ids)))
        if attachment_ids is not None:
            attachment_ids = list(map(str, attachment_ids))
            params['attachment_ids'] = attachment_ids
            options_log.append('Attachment IDs: {}'.format(', '.join(attachment_ids)))
        if fields is not None:
            params['include_fields'] = fields
        # attachment data doesn't get pulled by default
        if not get_data:
            params['exclude_fields'] = ['data']

        super().__init__(service=service, command='Bug.attachments', params=params)

        self.options = options_log
        self.ids = ids
        self.attachment_ids = attachment_ids

    def parse(self, data, *args, **kw):
        if self.ids:
            bugs = next(data)['bugs']
            for i in self.ids:
                yield [self.service.attachment(**attachment) for attachment in bugs[str(i)]]

        if self.attachment_ids:
            attachments = next(data)['attachments']
            files = []
            try:
                for i in self.attachment_ids:
                    files.append(self.service.attachment(**attachments[str(i)]))
            except KeyError:
                raise BiteError('invalid attachment ID: {}'.format(i))
            yield files


@command('history', BugzillaRpc)
@request(BugzillaRpc)
class _HistoryRequest(RPCRequest):
    def __init__(self, ids, created=None, fields=None, service=None, **kw):
        if not ids:
            raise ValueError('No bug ID(s) specified')

        params = {}
        options_log = []

        if ids is not None:
            ids = list(map(str, ids))
            params['ids'] = ids
            options_log.append('IDs: {}'.format(', '.join(ids)))
        if created is not None:
            params['new_since'] = created
            options_log.append('Created: {} (since {} UTC)'.format(
                created[0], parsetime(created[1])))
        if fields is not None:
            params['include_fields'] = fields

        super().__init__(service=service, command='Bug.history', params=params)
        self.options = options_log

    def parse(self, data, *args, **kw):
        bugs = next(data)['bugs']
        for b in bugs:
            yield [BugzillaEvent(change=x, id=b['id'], alias=b['alias'], count=i)
                   for i, x in enumerate(b['history'], start=1)]


@command('fields', BugzillaRpc)
@request(BugzillaRpc)
class _FieldsRequest(FieldsRequest, RPCRequest):
    def __init__(self, *args, **kw):
        super().__init__(command='Bug.fields', *args, **kw)
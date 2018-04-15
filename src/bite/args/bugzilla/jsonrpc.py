from . import Bugzilla4_4_Opts, Bugzilla5_0_Opts, Bugzilla5_2_Opts


class Bugzilla4_4JsonrpcOpts(Bugzilla4_4_Opts):

    _service = 'bugzilla4.4-jsonrpc'


class Bugzilla5_0JsonrpcOpts(Bugzilla5_0_Opts):

    _service = 'bugzilla5.0-jsonrpc'


class Bugzilla5_2JsonrpcOpts(Bugzilla5_2_Opts):

    _service = 'bugzilla5.2-jsonrpc'

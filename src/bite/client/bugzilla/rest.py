from . import Bugzilla


class Bugzilla5_0Rest(Bugzilla):
    """CLI for Bugzilla 5.0 REST interface."""

    _service = 'bugzilla5.0-rest'


class Bugzilla5_2Rest(Bugzilla):
    """CLI for Bugzilla 5.2 REST interface."""

    _service = 'bugzilla5.2-rest'

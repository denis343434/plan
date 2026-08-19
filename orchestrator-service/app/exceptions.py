class UpstreamServiceError(Exception):
    """Base class for errors talking to any of the three downstream services."""


class DataServiceError(UpstreamServiceError):
    pass


class DataServiceNotFoundError(DataServiceError):
    pass


class ParserServiceError(UpstreamServiceError):
    pass


class MessagingServiceError(UpstreamServiceError):
    pass

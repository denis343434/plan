class DataServiceError(Exception):
    pass


class DataServiceNotFoundError(DataServiceError):
    pass


class NoAccountAvailableError(DataServiceError):
    pass

"""Domain errors surfaced consistently by repositories and the API."""


class WorkflowError(Exception):
    """Base workflow error."""


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowConflictError(WorkflowError):
    pass


class InvalidTransitionError(WorkflowError):
    pass


class WorkflowAuthorizationError(WorkflowError):
    pass

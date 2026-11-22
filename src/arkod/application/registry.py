class UnknownOperationError(Exception):
    def __init__(self, operation: str):
        self.code = "unknown_operation"
        self.operation = operation
        self.message = f"Unknown operation: {operation}"
        super().__init__(self.message)


class Registry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")

        self._tools[tool.name] = tool

    def get(self, operation: str):
        if operation not in self._tools:
            raise UnknownOperationError(operation)

        return self._tools[operation]
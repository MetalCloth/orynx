"""Carry execution status without changing the text tool interface."""


class ToolResult(str):
    def __new__(cls, text: str, *, success: bool = True):
        result = super().__new__(cls, text)
        result.success = success
        return result

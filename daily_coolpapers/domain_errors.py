from typing import Any


class InvestmentThemeNotFoundError(LookupError):
    pass


class ArchivedThemeError(ValueError):
    pass


class ResearchEntityNotFoundError(LookupError):
    pass


class ResearchEntityConflictError(ValueError):
    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        super().__init__('作者或机构已存在或已归档，请核对后明确复用或先恢复；本次未保存任何改动')

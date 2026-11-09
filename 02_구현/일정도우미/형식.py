"""공개 오류와 엄격한 모델/사용자 입력 형식."""
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, Field

class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)

class EventDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: StrictStr = Field(max_length=300)
    start_date: StrictStr | None
    end_date: StrictStr | None
    start_time: StrictStr | None
    end_time: StrictStr | None
    all_day: StrictBool
    location: StrictStr = Field(max_length=1000)
    evidence: list[StrictStr] = Field(max_length=20)
    ambiguities: list[StrictStr] = Field(max_length=20)

class Extraction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    events: list[EventDraft] = Field(max_length=5)

EDIT_FIELDS = frozenset({'title', 'start_date', 'end_date', 'start_time', 'end_time', 'all_day', 'location'})

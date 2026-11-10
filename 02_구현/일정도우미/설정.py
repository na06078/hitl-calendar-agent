from dataclasses import dataclass, field
from pathlib import Path

class TimezoneSettingError(ValueError):
    """잘못된 실제 입력값을 노출하지 않고 시간대 설정 오류를 구분합니다."""

@dataclass(frozen=True)
class Settings:
    project_root: Path
    api_key: str = field(default='', repr=False)
    model: str = 'gpt-5.6-luna'
    client_file: str = '비공개/구글_클라이언트.json'
    token_file: str = '비공개/구글_토큰.json'
    calendar_id: str = ''
    timezone: str = 'Asia/Seoul'
    data_dir: str = 'data'
    llm_call_limit: int | None = None
    max_input_chars: int = 12000
    max_output_tokens: int = 2500
    max_rejudgements: int = 2

    def __post_init__(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        object.__setattr__(self, 'project_root', Path(self.project_root).resolve())
        if self.model != 'gpt-5.6-luna':
            raise ValueError('허용 모델은 gpt-5.6-luna입니다.')
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            # 공급자/화면 경계에서 안전하게 정규화할 수 있도록 설정 오류로 통일합니다.
            raise TimezoneSettingError('APP_TIMEZONE의 지원되는 시간대 이름을 확인하세요.') from None
        if (self.llm_call_limit is not None and self.llm_call_limit < 0) or self.max_rejudgements < 0 or min(self.max_input_chars, self.max_output_tokens) < 1:
            raise ValueError('호출·입력 상한 설정을 확인하세요.')
        for value in (self.client_file, self.token_file, self.data_dir):
            self.path(value)

    @classmethod
    def load(cls, project_root):
        from dotenv import dotenv_values
        root = Path(project_root).resolve()
        # interpolate=False: ${OPENAI_API_KEY}도 상속 환경으로 확장하지 않습니다.
        values = dotenv_values(root / '.env', interpolate=False)
        mapping = {'api_key': ('OPENAI_API_KEY', ''), 'model': ('OPENAI_MODEL', 'gpt-5.6-luna'),
                   'client_file': ('GOOGLE_CLIENT_FILE', '비공개/구글_클라이언트.json'),
                   'token_file': ('GOOGLE_TOKEN_FILE', '비공개/구글_토큰.json'),
                   'calendar_id': ('GOOGLE_CALENDAR_ID', ''), 'timezone': ('APP_TIMEZONE', 'Asia/Seoul'),
                   'data_dir': ('DATA_DIR', 'data'), 'llm_call_limit': ('LLM_CALL_LIMIT', None),
                   'max_input_chars': ('LLM_MAX_INPUT_CHARS', 12000),
                   'max_output_tokens': ('LLM_MAX_OUTPUT_TOKENS', 2500),
                   'max_rejudgements': ('MAX_REJUDGEMENTS', 2)}
        kwargs = {}
        for field_name, (key, default) in mapping.items():
            value = values.get(key)
            if field_name == 'llm_call_limit':
                # None은 실제 제한 없음입니다. 사용 횟수 장부는 계속 보존합니다.
                kwargs[field_name] = None if value in (None, '') or value.strip().lower() == 'unlimited' else int(value)
                continue
            kwargs[field_name] = int(value) if isinstance(default, int) and value not in (None, '') else (default if value in (None, '') else value)
        return cls(root, **kwargs)

    def path(self, relative):
        value = Path(relative)
        target = (self.project_root / value).resolve()
        if not target.is_relative_to(self.project_root.resolve()) or target == self.project_root.resolve():
            raise ValueError('프로젝트 밖 경로는 허용하지 않습니다.')
        return target

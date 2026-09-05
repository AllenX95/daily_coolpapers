import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Iterable


class FormValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("；".join(f"{field}: {message}" for field, message in self.errors.items()))


def parse_required_text(value: Any, field: str) -> str:
    cleaned = "" if value is None else str(value).strip()
    if not cleaned:
        raise FormValidationError({field: "不能为空"})
    return cleaned


def parse_choice(value: Any, field: str, choices: set[str]) -> str:
    cleaned = parse_required_text(value, field)
    if cleaned not in choices:
        raise FormValidationError({field: "值不受支持"})
    return cleaned


def parse_int(
    value: Any,
    field: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    cleaned = "" if value is None else str(value).strip()
    if not cleaned:
        if default is None:
            raise FormValidationError({field: "不能为空"})
        parsed = default
    else:
        try:
            parsed = int(cleaned)
        except (TypeError, ValueError) as exc:
            raise FormValidationError({field: "必须是整数"}) from exc
    if minimum is not None and parsed < minimum:
        raise FormValidationError({field: f"不能小于 {minimum}"})
    if maximum is not None and parsed > maximum:
        raise FormValidationError({field: f"不能大于 {maximum}"})
    return parsed


def parse_optional_int(value: Any, field: str, *, minimum: int = 1) -> int | None:
    if value in {None, "", "None"}:
        return None
    return parse_int(value, field, minimum=minimum)


def parse_float(
    value: Any,
    field: str,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    cleaned = "" if value is None else str(value).strip()
    try:
        parsed = default if not cleaned else float(cleaned)
    except (TypeError, ValueError) as exc:
        raise FormValidationError({field: "必须是数字"}) from exc
    if not math.isfinite(parsed):
        raise FormValidationError({field: "必须是有限数字"})
    if minimum is not None and parsed < minimum:
        raise FormValidationError({field: f"不能小于 {minimum}"})
    if maximum is not None and parsed > maximum:
        raise FormValidationError({field: f"不能大于 {maximum}"})
    return parsed


def parse_bool(value: Any, field: str) -> bool:
    if value is None:
        return False
    cleaned = str(value).strip().lower()
    if cleaned in {"1", "true", "yes", "on"}:
        return True
    if cleaned in {"", "0", "false", "no", "off"}:
        return False
    raise FormValidationError({field: "必须是布尔值"})


def parse_json_object(value: Any, field: str) -> str:
    cleaned = "" if value is None else str(value).strip()
    if not cleaned:
        return "{}"
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise FormValidationError({field: "必须是合法 JSON"}) from exc
    if not isinstance(parsed, dict):
        raise FormValidationError({field: "必须是 JSON object"})
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class InvestmentThemeCommand:
    name: str
    normalized_name: str
    description: str

    @classmethod
    def from_values(cls, name: Any, description: Any = '') -> 'InvestmentThemeCommand':
        name = parse_required_text(name, 'name')
        description = str(description or '').strip()
        for field, value in (('name', name), ('description', description)):
            if '\x00' in value:
                raise FormValidationError({field: '不能包含空字符（NUL）'})
        if len(name) > 80:
            raise FormValidationError({'name': '不能超过 80 个字符'})
        if len(description) > 500:
            raise FormValidationError({'description': '不能超过 500 个字符'})
        normalized = ' '.join(unicodedata.normalize('NFKC', name).split()).casefold()
        if not normalized:
            raise FormValidationError({'name': '不能为空'})
        return cls(name, normalized, description)


def parse_theme_ids(values: Iterable[Any]) -> list[int]:
    return list(dict.fromkeys(parse_int(value, 'theme_ids', minimum=1, maximum=2**63-1) for value in values))


AUTHOR_CATEGORIES = {'academic': '学术', 'industry': '产业', 'hybrid': '跨界', 'unknown': '未知'}
ORGANIZATION_TYPES = {'university': '高校', 'research_institute': '研究机构', 'company': '企业', 'other': '其他'}


def research_text(value: Any, field: str, *, required: bool = False) -> str:
    text = parse_required_text(value, field) if required else str(value or '').strip()
    if '\x00' in text:
        raise FormValidationError({field: '不能包含空字符（NUL）'})
    return text


def normalized_research_name(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


@dataclass(frozen=True)
class ResearchEntityCommand:
    kind: str
    values: dict[str, str]

    @classmethod
    def from_form(cls, kind: str, form: Mapping[str, Any], *, creating: bool = False) -> 'ResearchEntityCommand':
        kind = parse_choice(kind, 'kind', {'author', 'organization'})
        prefix = kind + '_' if creating else ''
        name = research_text(form.get(prefix+'name'), prefix+'name', required=True)
        normalized = normalized_research_name(name)
        if not normalized:
            raise FormValidationError({prefix+'name': '不能为空'})
        values = {'name': name, 'normalized_name': normalized,
                  'notes': research_text(form.get(prefix+'notes'), prefix+'notes')}
        if kind == 'author':
            values['author_category'] = parse_choice(form.get('author_category') or 'unknown', 'author_category', set(AUTHOR_CATEGORIES))
        else:
            values['organization_type'] = parse_choice(form.get('organization_type'), 'organization_type', set(ORGANIZATION_TYPES))
            values['region'] = research_text(form.get(prefix+'region'), prefix+'region')
        return cls(kind, values)


@dataclass(frozen=True)
class TeamTrackingCommand:
    author: ResearchEntityCommand | int
    organization: ResearchEntityCommand | int
    notes: str

    @classmethod
    def from_form(cls, form: Mapping[str, Any]) -> 'TeamTrackingCommand':
        entities = {}
        for kind in ('author', 'organization'):
            mode = parse_choice(form.get(kind+'_mode'), kind+'_mode', {'existing', 'new'})
            entities[kind] = (parse_int(form.get(kind+'_id'), kind+'_id', minimum=1, maximum=2**63-1)
                              if mode == 'existing' else ResearchEntityCommand.from_form(kind, form, creating=True))
        return cls(**entities, notes=research_text(form.get('tracking_notes'), 'tracking_notes'))


SETTINGS_DEFAULTS: dict[str, Any] = {
    "cache.pdf_retention_days": 5,
    "cache.markdown_retention_days": 7,
    "cache.cleanup_on_start": True,
    "cache.cleanup_daily": True,
    "llm.abstract_concurrency": 4,
    "llm.abstract_retries": 2,
    "job_events.retention_days": 30,
    "crawler.missing_field_warning_rate": 0.0,
    "crawler.trust_env_proxy": False,
    "crawler.proxy_url": "",
    "llm.trust_env_proxy": False,
    "llm.pdf_download_timeout_seconds": 300,
    "llm.pdf_download_retries": 2,
    "scheduler.enabled": True,
    "scheduler.daily_times": "10:30,12:00",
}


@dataclass(frozen=True)
class SettingsCommand:
    values: dict[str, Any]

    @classmethod
    def from_form(cls, form: Mapping[str, Any]) -> "SettingsCommand":
        return cls(
            {
                "cache.pdf_retention_days": parse_int(
                    form.get("pdf_retention_days"),
                    "pdf_retention_days",
                    default=5,
                    minimum=0,
                ),
                "cache.markdown_retention_days": parse_int(
                    form.get("markdown_retention_days"),
                    "markdown_retention_days",
                    default=7,
                    minimum=0,
                ),
                "cache.cleanup_on_start": parse_bool(form.get("cleanup_on_start"), "cleanup_on_start"),
                "cache.cleanup_daily": parse_bool(form.get("cleanup_daily"), "cleanup_daily"),
                "llm.abstract_concurrency": parse_int(
                    form.get("abstract_concurrency"),
                    "abstract_concurrency",
                    default=4,
                    minimum=1,
                    maximum=20,
                ),
                "crawler.trust_env_proxy": parse_bool(
                    form.get("crawler_trust_env_proxy"),
                    "crawler_trust_env_proxy",
                ),
                "llm.abstract_retries": parse_int(form.get('abstract_retries'), 'abstract_retries', default=2, minimum=0, maximum=5),
                "job_events.retention_days": parse_int(form.get('event_retention_days'), 'event_retention_days', default=30, minimum=1, maximum=3650),
                "crawler.missing_field_warning_rate": parse_float(form.get('missing_field_warning_rate'), 'missing_field_warning_rate', default=0.0, minimum=0, maximum=1),
                "crawler.proxy_url": str(form.get("crawler_proxy_url") or "").strip(),
                "llm.trust_env_proxy": parse_bool(form.get("llm_trust_env_proxy"), "llm_trust_env_proxy"),
                "llm.pdf_download_timeout_seconds": parse_int(
                    form.get("pdf_download_timeout_seconds"),
                    "pdf_download_timeout_seconds",
                    default=300,
                    minimum=30,
                ),
                "llm.pdf_download_retries": parse_int(
                    form.get("pdf_download_retries"),
                    "pdf_download_retries",
                    default=2,
                    minimum=0,
                    maximum=5,
                ),
                "scheduler.enabled": parse_bool(form.get("scheduler_enabled"), "scheduler_enabled"),
                "scheduler.daily_times": str(form.get("scheduler_daily_times") or "10:30,12:00").strip(),
            }
        )

"""Pydantic request models for the versioned JSON API."""

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionedRequest(StrictRequest):
    expected_version: StrictInt = Field(gt=0)


class ChannelTestRequest(VersionedRequest):
    expected_content_version: Optional[StrictInt] = Field(default=None, gt=0)


class TargetValidationRequest(StrictRequest):
    booking_url: str = Field(min_length=1, max_length=4096)


class TargetRevalidationRequest(VersionedRequest):
    booking_url: str = Field(min_length=1, max_length=4096)


class MessageContent(StrictRequest):
    preset: Literal['standard', 'compact', 'custom'] = 'standard'
    fields: List[Literal['job_name','practitioner','practice','earliest_appointment','check_time','time_zone','booking_link']] = Field(default_factory=lambda: ['practitioner','practice','earliest_appointment','booking_link'], min_length=1, max_length=7)
    silent: StrictBool = False

    @field_validator('fields')
    @classmethod
    def unique_fields(cls, fields):
        if len(set(fields)) != len(fields):
            raise ValueError('Message fields must be unique')
        return fields


class NotificationPreviewRequest(StrictRequest):
    channel_type: Literal['telegram','ntfy','email','webhook']
    message_content: Optional[MessageContent] = None


class QuietHoursPreviewRequest(StrictRequest):
    enabled: StrictBool
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    time_zone: str = Field(min_length=1, max_length=100)


class JobCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=120)
    target_urls: List[str] = Field(min_length=1, max_length=100)
    interval_seconds: int = Field(default=300, ge=300, le=86400)
    date_mode: Literal["first_available", "custom"] = "first_available"
    horizon_days: int = Field(default=15, ge=1, le=365)
    earliest_date: Optional[date] = None
    latest_date: Optional[date] = None
    time_zone: str = "Europe/Berlin"
    insurance_sector: Literal["public", "private"] = "public"
    telehealth: bool = False
    message_content: Optional[MessageContent] = None
    quiet_hours_enabled: StrictBool = False
    quiet_hours_start: str = Field(default="22:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_hours_end: str = Field(default="07:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    telegram_enabled: Optional[bool] = None
    notification_channel_ids: List[str] = Field(default_factory=list,max_length=100)

    @field_validator("target_urls")
    @classmethod
    def distinct_urls(cls, urls):
        if len(set(url.strip() for url in urls)) != len(urls):
            raise ValueError("Target URLs must be unique")
        return [url.strip() for url in urls]

    @model_validator(mode="after")
    def valid_date_rule(self):
        if self.date_mode == "custom":
            if self.earliest_date is None or self.latest_date is None:
                raise ValueError("Custom date mode requires earliest_date and latest_date")
            if self.earliest_date > self.latest_date:
                raise ValueError("earliest_date must be on or before latest_date")
            if (self.latest_date - self.earliest_date).days + 1 > 366:
                raise ValueError("Custom date range must not exceed 366 calendar dates")
        elif self.earliest_date is not None or self.latest_date is not None:
            raise ValueError("Use earliest_date and latest_date only with custom date mode")
        from app.quiet_hours import validate_quiet_hours
        validate_quiet_hours(self.quiet_hours_enabled, self.quiet_hours_start, self.quiet_hours_end)
        return self


class JobUpdateRequest(VersionedRequest):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    target_urls: Optional[List[str]] = Field(default=None, min_length=1, max_length=100)
    interval_seconds: Optional[int] = Field(default=None, ge=300, le=86400)
    date_mode: Optional[Literal["first_available", "custom"]] = None
    horizon_days: Optional[int] = Field(default=None, ge=1, le=365)
    earliest_date: Optional[date] = None
    latest_date: Optional[date] = None
    time_zone: Optional[str] = None
    insurance_sector: Optional[Literal["public", "private"]] = None
    telehealth: Optional[bool] = None
    message_content: Optional[MessageContent] = None
    quiet_hours_enabled: Optional[StrictBool] = None
    quiet_hours_start: Optional[str] = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_hours_end: Optional[str] = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    telegram_enabled: Optional[bool] = None
    notification_channel_ids: Optional[List[str]] = Field(default=None,max_length=100)

    @field_validator("target_urls")
    @classmethod
    def distinct_urls(cls, urls):
        if urls is not None:
            urls = [url.strip() for url in urls]
            if not urls or len(set(urls)) != len(urls):
                raise ValueError("Target URLs must be non-empty and unique")
        return urls

    @model_validator(mode="after")
    def supplied_values_are_not_null(self):
        nullable = {"earliest_date", "latest_date", "message_content"}
        for name in self.model_fields_set - nullable:
            if getattr(self, name) is None:
                raise ValueError(name + " cannot be null")
        return self


class SettingsUpdateRequest(VersionedRequest):
    message_content: Optional[MessageContent] = None
    default_interval_seconds: Optional[int] = Field(default=None, ge=300, le=86400)
    request_spacing_seconds: Optional[float] = Field(default=None, ge=3, le=120)

    @model_validator(mode="after")
    def supplied_values_are_not_null(self):
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(name + " cannot be null")
        return self


class ChannelCreateRequest(StrictRequest):
    type: Literal['telegram','ntfy','webhook','email'] = 'telegram'
    name: str = Field(min_length=1,max_length=120)
    enabled: bool = True
    token_action: Literal['replace','clear'] = 'replace'
    chat_action: Literal['replace','clear'] = 'replace'
    bot_token: Optional[str] = Field(default=None,max_length=200)
    chat_id: Optional[str] = Field(default=None,max_length=200)
    endpoint_action: Literal['replace','clear'] = 'replace'
    endpoint: Optional[str] = Field(default=None,max_length=4096)
    auth_type: Literal['none','bearer','basic'] = 'none'
    auth_action: Literal['keep','replace','clear'] = 'keep'
    auth_token: Optional[str] = Field(default=None,max_length=2000)
    auth_username: Optional[str] = Field(default=None,max_length=200)
    auth_password: Optional[str] = Field(default=None,max_length=2000)
    ntfy_priority: StrictInt = Field(default=3,ge=1,le=5)
    recipient_action: Literal['replace','clear'] = 'replace'
    recipient: Optional[str] = Field(default=None,max_length=254)


class ChannelUpdateRequest(VersionedRequest):
    name: Optional[str] = Field(default=None,min_length=1,max_length=120)
    enabled: Optional[bool] = None
    token_action: Literal['keep','replace','clear'] = 'keep'
    chat_action: Literal['keep','replace','clear'] = 'keep'
    bot_token: Optional[str] = Field(default=None,max_length=200)
    chat_id: Optional[str] = Field(default=None,max_length=200)
    endpoint_action: Literal['keep','replace','clear'] = 'keep'
    endpoint: Optional[str] = Field(default=None,max_length=4096)
    auth_type: Optional[Literal['none','bearer','basic']] = None
    auth_action: Literal['keep','replace','clear'] = 'keep'
    auth_token: Optional[str] = Field(default=None,max_length=2000)
    auth_username: Optional[str] = Field(default=None,max_length=200)
    auth_password: Optional[str] = Field(default=None,max_length=2000)
    ntfy_priority: Optional[StrictInt] = Field(default=None,ge=1,le=5)
    recipient_action: Literal['keep','replace','clear'] = 'keep'
    recipient: Optional[str] = Field(default=None,max_length=254)
    recover_failed: bool = False

    @model_validator(mode='after')
    def nonnull(self):
        if any(getattr(self,key) is None for key in self.model_fields_set & {'name','enabled','auth_type','ntfy_priority'}):
            raise ValueError('Channel fields cannot be null')
        return self


class ChannelDeleteRequest(VersionedRequest):
    confirmed: Literal[True]


class SmtpTransportUpdateRequest(VersionedRequest):
    expected_impact_token: Optional[str] = Field(default=None, min_length=64, max_length=64, pattern='^[a-f0-9]{64}$')
    enabled: bool
    host: str = Field(default='', max_length=253)
    port: StrictInt = Field(default=587, ge=1, le=65535)
    tls_mode: Literal['starttls','implicit_tls'] = 'starttls'
    sender_name: str = Field(default='', max_length=120)
    sender_email_action: Literal['keep','replace','clear'] = 'keep'
    sender_email: Optional[str] = Field(default=None, max_length=254)
    username_action: Literal['keep','replace','clear'] = 'keep'
    username: Optional[str] = Field(default=None, max_length=200)
    password_action: Literal['keep','replace','clear'] = 'keep'
    password: Optional[str] = Field(default=None, max_length=2000)
    recover_failed: bool = False

    @model_validator(mode='after')
    def actions_match_values(self):
        for action, value in ((self.sender_email_action, self.sender_email),
                              (self.username_action, self.username),
                              (self.password_action, self.password)):
            if (action == 'replace') != (value is not None):
                raise ValueError('Secret action and value must agree')
        return self

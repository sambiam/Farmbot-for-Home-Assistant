"""Minimal stand-in for homeassistant.exceptions."""


class HomeAssistantError(Exception):
    """Stand-in for homeassistant.exceptions.HomeAssistantError.

    Mirrors real Home Assistant's translation-key based construction: when
    no positional message is given, the translation_key is used as a
    fallback message so ``str(err)`` is still useful in tests and logs.
    """

    def __init__(
        self,
        *args,
        translation_domain: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: dict | None = None,
    ) -> None:
        if not args and translation_key:
            args = (translation_key,)
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


class ServiceValidationError(HomeAssistantError):
    """Stand-in for homeassistant.exceptions.ServiceValidationError."""

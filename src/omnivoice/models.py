"""Session-only selection of configured AI model profiles."""

from __future__ import annotations

from dataclasses import dataclass

from omnivoice.config import AgentConfig


class ModelSelectionError(ValueError):
    """Raised when a requested model alias is not configured."""


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """An immutable model choice suitable for request-time snapshots."""

    alias: str
    selector: str


class ModelRegistry:
    """Own the active model selection for one OmniVoice process."""

    def __init__(self, config: AgentConfig) -> None:
        self._models = dict(config.models)
        self._default_alias = config.default_model
        self._current_alias = config.default_model

    @property
    def default_alias(self) -> str | None:
        return self._default_alias

    @property
    def current_alias(self) -> str | None:
        return self._current_alias

    @property
    def configured(self) -> tuple[ModelSelection, ...]:
        """Return profiles in their YAML declaration order."""

        return tuple(ModelSelection(alias, selector) for alias, selector in self._models.items())

    def snapshot(self) -> ModelSelection | None:
        """Freeze the active selection for one future agent request."""

        if self._current_alias is None:
            return None
        return ModelSelection(self._current_alias, self._models[self._current_alias])

    def select(self, alias: str) -> tuple[ModelSelection, bool]:
        """Select an alias, returning the selection and whether it changed."""

        try:
            selector = self._models[alias]
        except KeyError as exc:
            raise ModelSelectionError(alias) from exc
        changed = alias != self._current_alias
        self._current_alias = alias
        return ModelSelection(alias, selector), changed

    def describe_status(self) -> str:
        selection = self.snapshot()
        if selection is None:
            return "agent_model=not configured"
        return f"agent_model={selection.alias} ({selection.selector})"

"""Session-only selection of configured AI model profiles."""

from __future__ import annotations

from dataclasses import dataclass

from omnivoice.config import (
    DEFAULT_MODEL_INPUT_TOKEN_BUDGET,
    AgentConfig,
    ModelProfileConfig,
)


class ModelSelectionError(ValueError):
    """Raised when a requested model alias is not configured."""


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """An immutable model choice suitable for request-time snapshots."""

    alias: str
    selector: str
    input_token_budget: int = DEFAULT_MODEL_INPUT_TOKEN_BUDGET


class ModelRegistry:
    """Own the active model selection for one OmniVoice process."""

    def __init__(self, config: AgentConfig) -> None:
        self._models = {
            alias: self._normalize_profile(profile)
            for alias, profile in config.models.items()
        }
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

        return tuple(
            ModelSelection(alias, selector, budget)
            for alias, (selector, budget) in self._models.items()
        )

    def snapshot(self) -> ModelSelection | None:
        """Freeze the active selection for one future agent request."""

        if self._current_alias is None:
            return None
        selector, budget = self._models[self._current_alias]
        return ModelSelection(self._current_alias, selector, budget)

    def select(self, alias: str) -> tuple[ModelSelection, bool]:
        """Select an alias, returning the selection and whether it changed."""

        try:
            selector, budget = self._models[alias]
        except KeyError as exc:
            raise ModelSelectionError(alias) from exc
        changed = alias != self._current_alias
        self._current_alias = alias
        return ModelSelection(alias, selector, budget), changed

    def describe_status(self) -> str:
        selection = self.snapshot()
        if selection is None:
            return "agent_model=not configured"
        return f"agent_model={selection.alias} ({selection.selector})"

    @staticmethod
    def _normalize_profile(
        profile: str | ModelProfileConfig,
    ) -> tuple[str, int]:
        if isinstance(profile, str):
            return profile, DEFAULT_MODEL_INPUT_TOKEN_BUDGET
        return profile.selector, profile.input_token_budget

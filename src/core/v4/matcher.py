"""Linear multi-pattern concept matching for frozen knowledge snapshots."""

from __future__ import annotations

import unicodedata
from collections import deque
from threading import RLock
from typing import Any, Dict, Iterable, Mapping, Sequence


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _word_character(value: str) -> bool:
    return value == "_" or value.isalnum()


class AhoConceptMatcher:
    def __init__(self, forms: Mapping[str, Iterable[str]]):
        self._next: list[Dict[str, int]] = [{}]
        self._failure: list[int] = [0]
        self._outputs: list[list[tuple[str, tuple[str, ...]]]] = [[]]
        for raw_form, concept_ids in sorted(forms.items()):
            form = _normalized(raw_form)
            ids = tuple(sorted({str(value) for value in concept_ids if str(value)}))
            if not form or not ids:
                continue
            state = 0
            for character in form:
                child = self._next[state].get(character)
                if child is None:
                    child = len(self._next)
                    self._next[state][character] = child
                    self._next.append({})
                    self._failure.append(0)
                    self._outputs.append([])
                state = child
            self._outputs[state].append((form, ids))
        self._build_failures()

    @classmethod
    def from_snapshot(cls, snapshot: Sequence[Mapping[str, Any]]):
        forms: Dict[str, set[str]] = {}
        for concept in snapshot:
            concept_id = str(concept.get("id") or "")
            if not concept_id:
                continue
            for value in concept.get("forms", []) or []:
                form = str(value).strip()
                if form:
                    forms.setdefault(form, set()).add(concept_id)
        return cls(forms)

    def _build_failures(self) -> None:
        queue: deque[int] = deque()
        for child in self._next[0].values():
            queue.append(child)
        while queue:
            state = queue.popleft()
            for character, child in self._next[state].items():
                queue.append(child)
                fallback = self._failure[state]
                while fallback and character not in self._next[fallback]:
                    fallback = self._failure[fallback]
                self._failure[child] = self._next[fallback].get(character, 0)
                inherited = self._outputs[self._failure[child]]
                if inherited:
                    self._outputs[child].extend(inherited)

    @staticmethod
    def _has_word_boundaries(text: str, start: int, end: int, form: str) -> bool:
        if _word_character(form[0]) and start > 0 and _word_character(text[start - 1]):
            return False
        if _word_character(form[-1]) and end < len(text) and _word_character(text[end]):
            return False
        return True

    def match(self, text: str) -> tuple[str, ...]:
        normalized = _normalized(text)
        state = 0
        matched: set[str] = set()
        for index, character in enumerate(normalized):
            while state and character not in self._next[state]:
                state = self._failure[state]
            state = self._next[state].get(character, 0)
            for form, concept_ids in self._outputs[state]:
                end = index + 1
                start = end - len(form)
                if self._has_word_boundaries(normalized, start, end, form):
                    matched.update(concept_ids)
        return tuple(sorted(matched))


class ConceptMatcherCache:
    _lock = RLock()
    _cache: Dict[str, AhoConceptMatcher] = {}
    _builds = 0
    _max_entries = 8

    @classmethod
    def get(
        cls, signature: str, snapshot: Sequence[Mapping[str, Any]]
    ) -> AhoConceptMatcher:
        with cls._lock:
            cached = cls._cache.get(signature)
            if cached is not None:
                return cached
            matcher = AhoConceptMatcher.from_snapshot(snapshot)
            cls._cache[signature] = matcher
            cls._builds += 1
            while len(cls._cache) > cls._max_entries:
                cls._cache.pop(next(iter(cls._cache)))
            return matcher

    @classmethod
    def build_count(cls) -> int:
        with cls._lock:
            return cls._builds

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._cache.clear()
            cls._builds = 0

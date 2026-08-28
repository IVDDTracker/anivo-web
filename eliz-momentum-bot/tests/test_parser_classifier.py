"""Tweet parsing + signal classification (spec §3/§4 tests)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.core.domain import Direction, SignalAction, SignalStage, TweetEvent, TweetKind
from src.twitter.classifier import LlmVerdict, PhraseEdgeTable, SignalClassifier
from src.twitter.parser import extract_candidates

KNOWN = {"BTC", "ETH", "TAO", "SOL", "DOGE", "OP"}


def tweet(text: str, **kw) -> TweetEvent:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    defaults = dict(tweet_id="1", author_id="42", text=text, kind=TweetKind.ORIGINAL,
                    created_at=now, received_at=now)
    defaults.update(kw)
    return TweetEvent(**defaults)


class TestParser:
    def test_cashtag_and_hashtag(self):
        assert extract_candidates("$TAO breaking out, also #DOGE") == ["TAO", "DOGE"]

    def test_bare_token_requires_whitelist(self):
        assert extract_candidates("TAO looks strong", KNOWN) == ["TAO"]
        assert extract_candidates("TAO looks strong") == []  # no whitelist → no bare match

    def test_random_words_not_coins(self):
        assert extract_candidates("I am SO BULLISH NOW GUYS", KNOWN) == []
        assert extract_candidates("going LONG here", KNOWN) == []  # jargon stopword

    def test_dollar_long_is_a_coin_but_word_long_is_not(self):
        assert extract_candidates("$LONG is pumping", KNOWN) == ["LONG"]

    def test_dedup_and_order(self):
        assert extract_candidates("$TAO $tao #TAO first, $ETH second") == ["TAO", "ETH"]


class TestRuleClassifier:
    @pytest.fixture
    def clf(self):
        return SignalClassifier()

    async def test_confirmed_buy(self, clf):
        c = await clf.classify(tweet("Bought $TAO here, size heavy"), ["TAO"])
        assert c.is_trade_signal and c.signal_stage == SignalStage.CONFIRMED
        assert c.direction == Direction.LONG and c.action == SignalAction.BUY
        assert c.symbol == "TAO" and c.confidence > 0.5
        assert "bought" in c.matched_phrases

    async def test_early_signal_is_not_no_trade(self, clf):
        c = await clf.classify(tweet("$TAO looks interesting"), ["TAO"])
        assert c.is_trade_signal  # spec: soft language must NOT be classified NO TRADE
        assert c.signal_stage == SignalStage.EARLY
        assert c.action == SignalAction.WATCH

    async def test_bearish(self, clf):
        c = await clf.classify(tweet("Shorted $OP on this bounce"), ["OP"])
        assert c.direction == Direction.SHORT and c.action == SignalAction.SELL

    async def test_no_symbol_no_signal(self, clf):
        c = await clf.classify(tweet("gm everyone, market looking spicy"), [])
        assert not c.is_trade_signal

    async def test_question_reduces_confidence(self, clf):
        statement = await clf.classify(tweet("long $TAO"), ["TAO"])
        question = await clf.classify(tweet("long $TAO?"), ["TAO"])
        assert question.confidence < statement.confidence

    async def test_negated_phrase_not_matched(self, clf):
        c = await clf.classify(tweet("I would not long $TAO here"), ["TAO"])
        assert c.signal_stage != SignalStage.CONFIRMED or not c.is_trade_signal

    async def test_ambiguous_without_llm_is_no_trade(self, clf):
        c = await clf.classify(tweet("$TAO 🌊🌊🌊"), ["TAO"])
        assert not c.is_trade_signal  # fail-safe default

    async def test_multiple_symbols_lower_confidence(self, clf):
        single = await clf.classify(tweet("bought $TAO"), ["TAO"])
        multi = await clf.classify(tweet("bought $TAO $SOL $DOGE"), ["TAO", "SOL", "DOGE"])
        assert multi.confidence < single.confidence

    async def test_latency_measured(self, clf):
        c = await clf.classify(tweet("bought $TAO"), ["TAO"])
        assert c.classification_latency_ms >= 0.0


class TestPhraseEdge:
    def test_edge_shrinks_small_samples(self, tmp_path):
        p = tmp_path / "edge.json"
        p.write_text(json.dumps({"looks interesting": {"edge": 0.9, "n": 2},
                                 "long": {"edge": 0.9, "n": 100}}))
        table = PhraseEdgeTable(p)
        assert abs(table.edge("looks interesting") - 0.5) < 0.1   # tiny sample ≈ neutral
        assert table.edge("long") > 0.8                            # big sample keeps edge
        assert table.edge("unheard phrase") == 0.5

    async def test_edge_modulates_confidence(self, tmp_path):
        p = tmp_path / "edge.json"
        p.write_text(json.dumps({"looks interesting": {"edge": 0.95, "n": 50}}))
        strong = SignalClassifier(edge_table=PhraseEdgeTable(p))
        neutral = SignalClassifier()
        t = tweet("$TAO looks interesting")
        assert (await strong.classify(t, ["TAO"])).confidence > \
               (await neutral.classify(t, ["TAO"])).confidence


class FakeLlm:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    async def classify(self, text, candidates):
        self.calls += 1
        return self.verdict


class TestLlmFallback:
    async def test_llm_only_called_for_ambiguous(self):
        llm = FakeLlm(LlmVerdict(is_trade_signal=True, symbol="TAO",
                                 direction=Direction.LONG,
                                 signal_stage=SignalStage.EARLY, confidence=0.8))
        clf = SignalClassifier(llm=llm)
        await clf.classify(tweet("bought $TAO"), ["TAO"])  # clear → no LLM call
        assert llm.calls == 0
        c = await clf.classify(tweet("$TAO 🌊"), ["TAO"])  # ambiguous → LLM
        assert llm.calls == 1 and c.is_trade_signal and c.used_llm

    async def test_llm_failure_means_no_trade(self):
        clf = SignalClassifier(llm=FakeLlm(None))
        c = await clf.classify(tweet("$TAO 🌊"), ["TAO"])
        assert not c.is_trade_signal

    async def test_low_llm_confidence_means_no_trade(self):
        llm = FakeLlm(LlmVerdict(is_trade_signal=True, symbol="TAO",
                                 direction=Direction.LONG,
                                 signal_stage=SignalStage.EARLY, confidence=0.3))
        clf = SignalClassifier(llm=llm)
        assert not (await clf.classify(tweet("$TAO 🌊"), ["TAO"])).is_trade_signal

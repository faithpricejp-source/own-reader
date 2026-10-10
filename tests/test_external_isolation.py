"""单测不能根据本机额度选择未替换的真实模型。"""
import pytest
import llm
import translate


def test_subscription_backends_are_blocked_by_default():
    for backend in ('claude', 'kimi', 'gemini', 'grok'):
        with pytest.raises(AssertionError, match='external backend'):
            llm.BACKENDS[backend]([{'role': 'user', 'content': 'fixture'}])

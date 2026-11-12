"""Thin wrapper around the Anthropic SDK used by extraction, the thesis and chat."""
import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

MODEL = os.environ.get('CLAUDE_MODEL', 'claude-opus-5-5')
FALLBACK_BETA = 'server-side-fallback-2026-07-01'


class LLMError(Exception):
    """A failure calling Claude, with a message suitable for showing to the user."""


def api_key_configured():
    return bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'))


_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3, timeout=600.0)
    return _client


def _friendly(exc):
    if isinstance(exc, anthropic.AuthenticationError):
        return 'The Anthropic API key was rejected. Check ANTHROPIC_API_KEY on the backend.'
    if isinstance(exc, anthropic.PermissionDeniedError):
        return 'The Anthropic API key does not have permission for this request.'
    if isinstance(exc, anthropic.RateLimitError):
        return 'The Anthropic API rate limit was reached. Wait a minute and try again.'
    if isinstance(exc, anthropic.APITimeoutError):
        return 'The request to Claude timed out. Try again.'
    if isinstance(exc, anthropic.APIConnectionError):
        return 'Could not connect to the Anthropic API.'
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f'The Anthropic API had a temporary problem (HTTP {exc.status_code}). Try again shortly.'
        return f'The Anthropic API rejected the request (HTTP {exc.status_code}): {exc.message}'
    return f'Unexpected error calling Claude: {exc}'


def complete(*, messages, system=None, schema=None, effort='medium', max_tokens=16000):
    """Run one request and return the response text (or the parsed object when `schema` is given).

    Streams the response so long extractions do not hit HTTP timeouts. Uses the
    server-side refusal fallback so a rare policy decline is retried on another
    model automatically; if the API rejects that beta option, the request is
    sent again without it.
    """
    if not api_key_configured():
        raise LLMError('ANTHROPIC_API_KEY is not configured on the backend.')

    kwargs = dict(model=MODEL, max_tokens=max_tokens, messages=messages)
    if system:
        kwargs['system'] = system
    output_config = {'effort': effort}
    if schema is not None:
        output_config['format'] = {'type': 'json_schema', 'schema': schema}
    kwargs['output_config'] = output_config

    def run(with_fallback):
        if with_fallback:
            stream_cm = client().beta.messages.stream(betas=[FALLBACK_BETA], fallbacks='default', **kwargs)
        else:
            stream_cm = client().messages.stream(**kwargs)
        with stream_cm as stream:
            return stream.get_final_message()

    try:
        try:
            message = run(with_fallback=True)
        except anthropic.BadRequestError as exc:
            logger.warning('Request with refusal fallback was rejected (%s); retrying without it.', exc.message)
            message = run(with_fallback=False)
    except anthropic.APIError as exc:
        raise LLMError(_friendly(exc)) from exc

    if message.stop_reason == 'refusal':
        raise LLMError('Claude declined to process this content.')
    if message.stop_reason == 'max_tokens':
        raise LLMError('The response was cut off before it finished (too much content for one request).')

    text = ''.join(block.text for block in message.content if block.type == 'text')
    if schema is None:
        return text
    try:
        return json.loads(text)
    except ValueError as exc:
        raise LLMError('Claude returned a response that was not valid JSON.') from exc


SEARCH_TOOL = 'web_search_20260209'


def search(prompt, *, system=None, max_uses=6, effort='medium', max_tokens=8000):
    """Ask Claude a question it answers by searching the web (Anthropic's hosted web search tool).
    Returns (answer_text, results) where results is a list of {'url', 'title'} seen in the searches."""
    if not api_key_configured():
        raise LLMError('ANTHROPIC_API_KEY is not configured on the backend.')
    messages = [{'role': 'user', 'content': prompt}]
    kwargs = dict(model=MODEL, max_tokens=max_tokens, output_config={'effort': effort},
                  tools=[{'type': SEARCH_TOOL, 'name': 'web_search', 'max_uses': max_uses}])
    if system:
        kwargs['system'] = system

    text, results = [], []
    try:
        for _ in range(4):                      # a long search can pause and be resumed
            with client().messages.stream(messages=messages, **kwargs) as stream:
                message = stream.get_final_message()
            for block in message.content:
                if block.type == 'text':
                    text.append(block.text)
                elif block.type == 'web_search_tool_result' and isinstance(block.content, list):
                    for item in block.content:
                        url = getattr(item, 'url', None)
                        if url:
                            results.append({'url': url, 'title': getattr(item, 'title', '') or ''})
            if message.stop_reason != 'pause_turn':
                break
            messages = [messages[0], {'role': 'assistant', 'content': message.content}]
    except anthropic.APIError as exc:
        raise LLMError(_friendly(exc)) from exc
    if message.stop_reason == 'refusal':
        raise LLMError('Claude declined to run this search.')
    return ''.join(text), results

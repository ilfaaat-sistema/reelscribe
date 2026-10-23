"""Статический контракт workflow без GitHub API и dispatch."""
import re
from pathlib import Path

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/carousel-caption-translations.yml"


def source() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def step(text: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    assert text.count(marker) == 1
    value = text.split(marker, 1)[1]
    return value.split("\n      - name:", 1)[0]


def test_triggers_guards_permissions_and_concurrency_are_preserved():
    text = source()
    dispatch = text.split("  workflow_dispatch:\n", 1)[1].split("  schedule:\n", 1)[0]
    assert dispatch == (
        "    inputs:\n"
        "      max_posts:\n"
        "        description: Предел одного прохода (1–500)\n"
        "        required: false\n"
        "        type: number\n"
        "        default: 500\n"
    )
    assert "  schedule:\n    - cron: '4,19,34,49 * * * *'\n" in text
    assert "  workflow_run:\n    workflows: ['Живые избранные карусели']\n    types: [completed]\n" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "github.event.workflow_run.head_branch == 'main'" in text
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in text
    permissions = text.split("permissions:\n", 1)[1].split("\nconcurrency:", 1)[0]
    assert set(re.findall(r"^  ([a-z]+): ([a-z]+)$", permissions, re.MULTILINE)) == {
        ("contents", "write"), ("actions", "write"),
    }
    assert "concurrency:\n  group: carousel-caption-bulk\n  cancel-in-progress: false\n" in text


def test_continuation_uses_only_exact_worker_output_and_own_workflow():
    text = source()
    queue = step(text, "Продолжить очередь перевода")
    assert "        id: queue\n" in queue
    assert "        run: python -m app.workers.carousel_caption_bulk\n" in queue
    max_posts_env = (
        "          CAROUSEL_CAPTION_BULK_MAX_POSTS: "
        "${{ github.event_name == 'workflow_dispatch' && toJSON(inputs.max_posts) || '500' }}"
    )
    assert max_posts_env in queue
    assert "${{ inputs.max_posts || 500 }}" not in queue

    handoff = step(text, "Передать незавершённую очередь следующему run")
    assert "        if: steps.queue.outputs.needs_continuation == 'true'\n" in handoff
    command = '        run: gh workflow run carousel-caption-translations.yml --repo "$GITHUB_REPOSITORY" --ref main\n'
    assert handoff.count(command) == 1
    assert "          GH_TOKEN: ${{ github.token }}\n" in handoff
    assert not any(value in handoff for value in ("http://", "https://", "--field", " -f ", "secrets."))
    assert "max_posts" not in handoff
    assert text.count("gh workflow run ") == 1

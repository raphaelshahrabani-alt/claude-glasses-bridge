"""`glasses profile`: a few questions that write the profile the voice agent reads on every call.

The profile (persona.md) is what makes the agent know you: your name, your businesses and what to watch
for in the news. It is plain text, so it can still be edited in Notepad afterwards.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "business-partner.md"

NEWS_RULE = (
    "When I ask what's new, for the news, or to be caught up, search the web for each business, competitor and "
    "topic above, the last day or two first. Lead with what changed and matters most, say where each item is from "
    "and how recent it is, and leave out what I already know. If nothing new turned up, say so instead of filling "
    "the gap."
)


def placeholders_left(text: str) -> bool:
    """True for a profile that is still the unfilled template (or empty)."""
    return not text.strip() or "<your name>" in text


def section(template: str, heading: str) -> str:
    """One '## heading' block of the template, without its heading line."""
    found = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", template, re.M | re.S)
    return found.group(1).strip() if found else ""


def tidy(text: str) -> str:
    """One line of plain text: no line breaks, and no colon that would be mistaken for a label."""
    return re.sub(r"\s+", " ", text.replace(":", " -")).strip()


def ask_list(prompt: str, ask: Callable[[str], str], say: Callable[[str], None], limit: int = 12) -> list[str]:
    items: list[str] = []
    say(prompt)
    while len(items) < limit:
        answer = tidy(ask("  > "))
        if not answer:
            break
        items.append(answer)
    return items


def interview(ask: Callable[[str], str], say: Callable[[str], None], zone: str = "", template: str | None = None) -> str:
    """Asks the questions and returns the profile text."""
    template = TEMPLATE.read_text(encoding="utf-8") if template is None else template
    say("A few questions, so the glasses know who you are. Press Enter to skip any of them.")
    say("You can run `glasses profile` again whenever something changes.")
    say("")
    name = tidy(ask("Your full name: "))
    nickname = tidy(ask("What should it call you? (Enter keeps your first name): "))
    if name and nickname and nickname.lower() != name.split()[0].lower():
        name = f"{name} ({nickname.split()[0]})"
    elif nickname and not name:
        name = nickname
    what = tidy(ask("What do you do? One or two lines: "))
    where = tidy(ask(f"Your time zone{f' (Enter keeps {zone})' if zone else ''}: ")) or zone
    say("")
    say("Your businesses. For each: its name, then what it does, then its website, then competitors or")
    say("names to watch in the news. Press Enter on the name when there are no more.")
    businesses: list[str] = []
    while len(businesses) < 10:
        business = tidy(ask("Business name: "))
        if not business:
            break
        does = tidy(ask(f"  What does {business} do? "))
        site = tidy(ask(f"  Website of {business}: "))
        watch = tidy(ask(f"  Competitors or names to watch for {business} (commas): "))
        line = f"- {business}: {does.rstrip('. ') or 'what it does is not written down yet'}."
        if site:
            line += f" Website: {site.rstrip('. ')}."
        if watch:
            line += f" Watch: {watch.rstrip('. ')}."
        businesses.append(line)
    say("")
    topics = ask_list("News topics you care about (an industry, a market, a rule that matters). One per line, then Enter on an empty line:",
                      ask, say)
    say("")
    projects = ask_list("Software projects it should work on (name, then a dash, then what it is). One per line, then Enter on an empty line:",
                        ask, say)
    say("")
    extra = tidy(ask("Anything else it should always know or do? (goals, how you like answers, people it will hear about): "))

    intro = template.split("## About me", 1)[0].strip()
    parts = [intro, "", "## About me", ""]
    parts.append(f"- Name: {name}" if name else "- Name:")
    if what:
        parts.append(f"- What I do: {what}")
    if where:
        parts.append(f"- Time zone: {where}")
    if extra:
        parts.append(f"- Also: {extra}")
    parts.append("")
    parts += ["## My businesses", ""]
    parts += businesses or ["- (none written down yet)"]
    parts += ["", "## News I care about", ""]
    parts += [f"- {topic}" for topic in topics] or ["- (nothing written down yet; ask me what to watch)"]
    parts += ["", NEWS_RULE, ""]
    if projects:
        parts += ["## My projects", ""]
        for project in projects:
            label, _, rest = project.partition(" - ")
            parts.append(f"- {label.strip()}: {rest.strip().rstrip('. ') or 'a software project'}.")
        parts += [""]
    parts += ["## New projects", "",
              "Create a new project in its own folder, named after it, inside the folder you work in. Start it with "
              "`git init`, a README saying what it is, and the simplest stack that does the job. Tell me the folder "
              "name when it's done.", ""]
    for heading in ("Email and calendar", "Always check with me first"):
        body = section(template, heading)
        if body:
            parts += [f"## {heading}", "", body, ""]
    return "\n".join(parts).rstrip() + "\n"


def run(path: Path, ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
        open_in_notepad: Callable[[Path], None] = lambda p: None, zone: str = "") -> int:
    """`glasses profile`: the questions the first time; afterwards, a choice."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if not placeholders_left(existing):
        say(f"You already have a profile ({path}).")
        choice = ask("Press Enter to keep it, E to edit it in Notepad, or R to answer the questions again: ").strip().lower()
        if choice == "e":
            open_in_notepad(path)
            say("Save it and close Notepad, then restart so it takes effect: glasses stop, then glasses start")
            return 0
        if choice != "r":
            say("Kept as it is.")
            return 0
        path.with_name(path.name + ".bak").write_text(existing, encoding="utf-8")
        say(f"(The old one is kept as {path.name}.bak.)")
        say("")
    text = interview(ask, say, zone=zone)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    say("")
    say(f"Saved to {path}. Here is what it will read on every call:")
    say("-" * 60)
    say(text)
    say("-" * 60)
    say("Restart so it takes effect: glasses stop, then glasses start. To change it by hand: glasses profile, then E.")
    return 0

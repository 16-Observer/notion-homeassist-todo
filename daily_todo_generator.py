# Daily Todo Generator v6 - AppDaemon App
#
# What's new vs v5:
#   - Proper Notion block rendering (heading_2, heading_3, to_do, bulleted_list_item)
#   - HA todo: clear and add are independent — a clear failure no longer blocks adds
#   - Quick Tasks: uses correct DB (349ea7b1...), correct field types (select not status),
#     correct priority order (Backlog→Low→Medium→High)
#   - Quick Tasks: Claude-extracted tasks are added OR their priority is bumped if
#     they already exist in the DB (no duplicates)
#   - Calendar: no HA/Notion calendar DB available — Claude extracts date events from
#     journal and writes them as a callout block in the day's Notion page
#   - New prompt structure matching the original ChatGPT workflow:
#       emotional state → stressors → learnings → therapy notes → ideas →
#       extracted tasks → calendar events → schedule
#   - Morning block simplified: only ADHD-critical items, max 5-6 total
#   - Hard cap of 5-6 items per schedule block
#
# Install: /addon_configs/a0d7b954_appdaemon/apps/daily_todo_generator.py
#
# Required apps.yaml keys:
#   notion_api_key:       !secret notion_api_key
#   claude_api_key:       !secret claude_api_key
#   claude_model:         claude-opus-4-6          (optional, default shown)
#   journal_root_id:      <notion page id>
#   caretaking_page_id:   <notion page id>
#   quick_task_db_id:     <notion db id>  → use fbf8b23d4ed74ba0bc73991baae428b6
#   project_dump_db_id:   <notion db id>
#   todo_entity:          todo.daily_schedule       (optional, default shown)

import hassapi as hass
import requests
import json
import re
import os
from datetime import datetime, timedelta

HA_API = "http://supervisor/core/api"

NOTION_VERSION = "2022-06-28"
CLAUDE_URL     = "https://api.anthropic.com/v1/messages"
NOTION_URL     = "https://api.notion.com/v1"

# Priority ladder for Quick Tasks — lowest to highest (matches Notion DB options)
PRIORITY_ORDER = ["Backlog", "Low", "Medium", "High"]


class DailyTodoGenerator(hass.Hass):

    def initialize(self):
        self.notion_key      = self.args.get("notion_api_key")
        self.claude_key      = self.args.get("claude_api_key")
        self.claude_model    = self.args.get("claude_model", "claude-opus-4-6")

        self.journal_root_id    = self.args.get("journal_root_id")
        self.caretaking_page_id = self.args.get("caretaking_page_id")
        self.quick_task_db_id   = self.args.get("quick_task_db_id")
        self.project_dump_db_id = self.args.get("project_dump_db_id")
        self.todo_entity        = self.args.get("todo_entity", "todo.daily_schedule")

        self.run_daily(self.generate_todo, "01:00:00")
        self.listen_event(self.manual_trigger, "generate_daily_todo")
        self.log("Daily Todo Generator v6 initialized.")

    # ─── Entry points ────────────────────────────────────────────────────────

    def manual_trigger(self, event_name, data, kwargs):
        self.log("Manual trigger received.")
        self.generate_todo({})

    def generate_todo(self, kwargs):
        self.log("Starting daily todo generation...")
        try:
            now         = datetime.now()
            yesterday   = now - timedelta(days=1)   # journal to read
            target_date = now                        # day we are planning for

            # Capture incomplete items BEFORE write_to_ha clears the list
            incomplete_items = self.get_incomplete_ha_items()

            journal_text    = self.get_journal(yesterday)
            caretaking_text = self.get_caretaking_tasks()
            quick_tasks     = self.get_quick_tasks()   # list of dicts
            project_tasks   = self.get_project_tasks() # plain text

            prompt   = self.build_prompt(target_date, journal_text, caretaking_text,
                                         quick_tasks, project_tasks, incomplete_items)
            response = self.call_claude(prompt)
            if not response:
                self.log("Claude returned empty response.", level="WARNING")
                return

            day_page_id = self.write_to_notion(response, target_date)
            self.write_to_ha(response)
            self.upsert_quick_tasks(response, quick_tasks)
            self.apply_quick_tasks_triage(response, quick_tasks)
            self.write_calendar_events_to_notion(response, day_page_id)

            self.log("Daily todo generation complete.")
            self.fire_event("daily_todo_generated", date=target_date.strftime("%m/%d"))

        except Exception as e:
            import traceback
            self.log("Error in generate_todo: {}\n{}".format(e, traceback.format_exc()),
                     level="ERROR")

    # ─── Notion HTTP helpers ──────────────────────────────────────────────────

    def n_headers(self):
        return {
            "Authorization":  "Bearer {}".format(self.notion_key),
            "Notion-Version": NOTION_VERSION,
            "Content-Type":   "application/json",
        }

    def n_get(self, path, params=None):
        r = requests.get("{}/{}".format(NOTION_URL, path),
                         headers=self.n_headers(), params=params)
        r.raise_for_status()
        return r.json()

    def n_post(self, path, body):
        r = requests.post("{}/{}".format(NOTION_URL, path),
                          headers=self.n_headers(), json=body)
        r.raise_for_status()
        return r.json()

    def n_patch(self, path, body):
        r = requests.patch("{}/{}".format(NOTION_URL, path),
                           headers=self.n_headers(), json=body)
        r.raise_for_status()
        return r.json()

    # ─── Text extraction ──────────────────────────────────────────────────────

    def extract_text(self, blocks):
        lines = []
        for block in blocks:
            bt   = block.get("type", "")
            rich = block.get(bt, {}).get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich)
            if text:
                lines.append(text)
            if block.get("has_children"):
                try:
                    children = self.n_get(
                        "blocks/{}/children".format(block["id"])
                    ).get("results", [])
                    sub = self.extract_text(children)
                    if sub:
                        lines.append(sub)
                except Exception:
                    pass
        return "\n".join(lines)

    def get_prop_title(self, props, key):
        try:
            return props[key]["title"][0]["plain_text"]
        except Exception:
            return ""

    def get_prop_select(self, props, key):
        try:
            return props[key]["select"]["name"]
        except Exception:
            return ""

    # ─── Data fetchers ────────────────────────────────────────────────────────

    def find_child_page(self, parent_id, title_match):
        results = self.n_get("blocks/{}/children".format(parent_id)).get("results", [])
        for b in results:
            if b.get("type") == "child_page":
                title = b.get("child_page", {}).get("title", "")
                if title == title_match or title.startswith(title_match):
                    return b["id"]
        return None

    def get_journal(self, date):
        try:
            year_str  = date.strftime("%Y")
            month_str = date.strftime("%B %Y")
            day_str   = date.strftime("%m/%d")

            year_id = self.find_child_page(self.journal_root_id, year_str)
            if not year_id:
                return "No journal year page found for {}.".format(year_str)

            month_id = self.find_child_page(year_id, month_str)
            if not month_id:
                return "No journal month page found for {}.".format(month_str)

            day_id = self.find_child_page(month_id, day_str)
            if not day_id:
                return "No journal entry found for {}.".format(day_str)

            blocks = self.n_get("blocks/{}/children".format(day_id)).get("results", [])
            return self.extract_text(blocks) or "Journal page was empty."
        except Exception as e:
            self.log("Error fetching journal: {}".format(e), level="WARNING")
            return "Could not read journal."

    def get_caretaking_tasks(self):
        try:
            blocks = self.n_get(
                "blocks/{}/children".format(self.caretaking_page_id)
            ).get("results", [])
            return self.extract_text(blocks) or "No caretaking tasks found."
        except Exception as e:
            self.log("Error fetching caretaking: {}".format(e), level="WARNING")
            return "Could not read caretaking tasks."

    def get_quick_tasks(self):
        """Returns a list of dicts so we can match/update them later.
        Status is a SELECT field (not a status-type), options: To Do, In Progress, Done, Cut.
        Priority is a SELECT field, options: Backlog, Low, Medium, High.
        """
        try:
            body = {
                "filter": {
                    "and": [
                        {"property": "Status", "select": {"does_not_equal": "Done"}},
                        {"property": "Status", "select": {"does_not_equal": "Cut"}},
                    ]
                },
                "sorts": [{"property": "Priority", "direction": "ascending"}],
                "page_size": 30,
            }
            results = self.n_post(
                "databases/{}/query".format(self.quick_task_db_id), body
            ).get("results", [])

            items = []
            for r in results:
                props = r.get("properties", {})
                items.append({
                    "id":            r["id"],
                    "task":          self.get_prop_title(props, "Task"),
                    "area":          self.get_prop_select(props, "Area"),
                    "time_estimate": self.get_prop_select(props, "Time Estimate"),
                    "priority":      self.get_prop_select(props, "Priority"),
                })
            return items
        except Exception as e:
            self.log("Error fetching quick tasks: {}".format(e), level="WARNING")
            return []

    def get_project_tasks(self):
        try:
            body = {
                "filter": {
                    "or": [
                        {"property": "Status", "status": {"equals": "Not started"}},
                        {"property": "Status", "status": {"equals": "In progress"}},
                    ]
                },
                "sorts": [{"property": "Priority", "direction": "ascending"}],
                "page_size": 15,
            }
            results = self.n_post(
                "databases/{}/query".format(self.project_dump_db_id), body
            ).get("results", [])

            items = []
            for r in results:
                props = r.get("properties", {})
                task  = self.get_prop_title(props, "Task")
                area  = self.get_prop_select(props, "Area")
                items.append("- {} ({})".format(task, area) if area else "- {}".format(task))
            return "\n".join(items) if items else "No active projects."
        except Exception as e:
            self.log("Error fetching projects: {}".format(e), level="WARNING")
            return "Could not read projects."

    def _ha_token(self):
        return os.environ.get("SUPERVISOR_TOKEN", "")

    def _ha_headers(self):
        return {
            "Authorization": "Bearer {}".format(self._ha_token()),
            "Content-Type": "application/json",
        }

    def _ha_get_todo_items(self):
        """Fetch current todo items from HA via REST API.
        Returns list of dicts with at least 'summary', 'uid', 'status' keys.
        """
        r = requests.post(
            "{}/services/todo/get_items".format(HA_API),
            headers=self._ha_headers(),
            json={"entity_id": self.todo_entity},
            params={"return_response": "true"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        # HA 2023.4+: {"service_response": {"todo.entity": {"items": [...]}}}
        if isinstance(data, dict) and "service_response" in data:
            return data["service_response"].get(self.todo_entity, {}).get("items", [])
        return []

    def _ha_remove_todo_item(self, uid):
        requests.post(
            "{}/services/todo/remove_item".format(HA_API),
            headers=self._ha_headers(),
            json={"entity_id": self.todo_entity, "item": uid},
            timeout=10,
        ).raise_for_status()

    def _ha_add_todo_item(self, summary):
        requests.post(
            "{}/services/todo/add_item".format(HA_API),
            headers=self._ha_headers(),
            json={"entity_id": self.todo_entity, "item": summary},
            timeout=10,
        ).raise_for_status()

    def get_incomplete_ha_items(self):
        """Return summaries of items not yet completed — called BEFORE we clear the list."""
        try:
            items = self._ha_get_todo_items()
            incomplete = [
                item.get("summary", "")
                for item in items
                if item.get("status", "needs_action") != "completed"
                and item.get("summary", "")
            ]
            self.log("Found {} incomplete items from yesterday.".format(len(incomplete)))
            return incomplete
        except Exception as e:
            self.log("Could not read incomplete items: {}".format(e), level="WARNING")
            return []

    # ─── Prompt ───────────────────────────────────────────────────────────────

    def build_prompt(self, target_date, journal_text, caretaking_text,
                     quick_tasks, project_tasks, incomplete_items=None):
        day_name = target_date.strftime("%A")
        date_str = target_date.strftime("%m/%d/%Y")

        quick_tasks_text = "\n".join(
            "- [{}] {} ({}, {})".format(
                t["priority"], t["task"], t["area"], t["time_estimate"]
            )
            for t in quick_tasks
        ) if quick_tasks else "No active quick tasks."

        incomplete_text = "\n".join(
            "- {}".format(i) for i in (incomplete_items or [])
        ) or "None — either everything got done or this is the first run."

        return """You are Austin's executive function assistant. Your job: analyze today's journal, triage what didn't get done, and produce a structured daily plan for tomorrow ({day_name}, {date_str}).

CONTEXT ABOUT AUSTIN:
- Has ADHD — tasks must be specific, broken into 5-15 min steps requiring zero decision-making.
- Runs a 9-acre homestead with ~40 chickens, geese, dogs, cats, and a large garden.
- Goes by Grace sometimes, wears diapers — routine self-care, never list as a task.
- Do NOT list "change diaper", "apply deodorant", or "get dressed" as tasks. These happen.
- ADHD friction points to always catch: brush teeth, drink water, feed animals.
- Morning block: SHORT. Only what ADHD makes easy to skip. 5-6 items MAX.
- Each time block: 5-6 items MAX. Fewer meaningful tasks beat a wall of tiny ones.

QUICK TASK RULES (important):
- Everything that needs tracking goes in Quick Tasks — even small, quick items — UNLESS it is a
  purely recurring routine (laundry, dishes, sweeping). Those live in Caretaking.
- If something is causing emotional friction (cluttered closet, pile that has no home, broken thing
  that keeps catching your eye), it goes in Quick Tasks regardless of how "small" it seems.
  The friction IS the reason to track it.
- High priority in Quick Tasks = has been pushed multiple times. Treat these seriously.

TODAY'S JOURNAL:
{journal_text}

INCOMPLETE FROM YESTERDAY (items that were scheduled but not checked off):
{incomplete_text}

CARETAKING TASKS (reference — pull what's relevant, don't dump the whole list):
{caretaking_text}

QUICK TASKS (active, sorted by priority — High first):
{quick_tasks_text}

PROJECTS (active):
{project_tasks}

---

Produce exactly these sections in this order:

## 📊 State of Mind
2-3 sentences on Austin's emotional state, energy level, and biggest thing weighing on them. Direct and warm.

## 😤 High-Friction Items
Things in the journal expressed with emotional weight — frustration, "this is driving me crazy", "I really need to", shame, avoidance, a thing that keeps catching their eye.
These get tracked in Quick Tasks at High priority regardless of how small they seem.
If none are detectable: write "None today."
Format each as a bullet: the thing + why it's friction + suggested first action (one small step).

## 💡 Stressors & Patterns
Recurring themes, stressors, or patterns from the journal worth naming.

## 🎯 Most Important Next Actions
Top 3 highest-leverage moves for tomorrow. Not schedule items — just the North Stars.

## 📚 Learnings
- Factual learnings or useful insights from today
- **Therapy notes:** emotional patterns, conflicts, or feelings worth exploring in a session

## 💭 Ideas & Questions
Random questions, ideas, or future-project seeds from the journal worth following up on.

## 🔄 Quick Tasks Triage
Review every HIGH and MEDIUM priority Quick Task. For each, decide:
- SCHEDULE — putting it on tomorrow's plan
- HOLD — relevant but not this week, keep as-is
- STUCK — has been at High for a while, call it out explicitly so Austin can decide to break it down, delegate, or cut it
- CUT — no longer relevant, should be marked Cut

Format EXACTLY (one per line, no extra text):
TASK NAME | ACTION | one-sentence reason
If no High/Medium tasks exist: write "None"

## ✅ Tasks Extracted from Journal
Concrete to-dos mentioned in the journal. Simple bullet list.
If already in Quick Tasks: mark "⚠️ Already tracked — priority will be bumped."

## 📅 Calendar Events to Create
Date-specific events Austin mentioned (appointments, visitors, deadlines).
Format EXACTLY (one per line):
- [YYYY-MM-DD HH:MM] [YYYY-MM-DD HH:MM] Event title
For all-day: - [YYYY-MM-DD] [YYYY-MM-DD] Event title
If none: write exactly "None"

## 📋 Quick Tasks to Add
Tasks and friction items NOT already in Quick Tasks that need tracking.
Include High-Friction Items from above unless they're already tracked.
Do NOT include recurring routines (laundry, dishes, feeding animals).
Format EXACTLY (one per line):
TASK | AREA | PRIORITY
Where AREA is one of: Homestead, Garden, House, Business, Personal, Creative, Automotive
Where PRIORITY is one of: Backlog, Low, Medium, High
If none: write exactly "None"

## 📅 Daily Schedule for {day_name}, {date_str}

INCOMPLETE ITEM TRIAGE RULES (apply before building the schedule):
- Animal care / ADHD-critical: always reschedule in Wake Up block, no debate
- High-Friction items: schedule tomorrow if small, or note them in the plan
- Already in Quick Tasks at High priority: do NOT reschedule — it's tracked, trust the system
- Journal shows low energy / hard day: drop non-critical incomplete items, let them go
- No journal signal: reschedule once silently; if it recurs again after that, it's stuck

**🌅 Wake Up — 9:00 AM**
- [ ] Brush teeth 🦷
- [ ] Drink a full glass of water 💧
- [ ] Feed dogs + check water bowls 🐕
- [ ] Walk to coop — feed and water chickens + geese 🐔
- [ ] Let chickens, geese, and roosters out
[Add 1 more only if a genuine must-do exists. Hard stop at 6.]

**☀️ 9:00 AM — 12:00 PM**
[5-6 items max. Pull from High priority Quick Tasks first.]

**🌤️ 1:00 PM — 3:00 PM**
[5-6 items max.]

**🌇 3:00 PM — 6:00 PM**
[5-6 items max.]

**🌙 6:00 PM — Bedtime**
[5-6 items max. Wind-down only — no new projects, nothing that requires outdoor work.]

SCHEDULE RULES:
- 5-6 items per block. Hard cap. No exceptions.
- Pull from Quick Tasks: High → Medium → Low. Don't skip High items.
- Use relevant emojis. Sub-group with *italic labels* (*Animals:*, *Outside:*, *House:*) when helpful.
- Low energy journal → lighter day. Momentum → more ambitious.
- Break tasks into smallest concrete step. Not "clean the closet" — "take a photo of the closet and drop it in Notion."

## 💬 Note from Your Assistant
1-2 sentences. Genuine and specific to what Austin is actually going through. No cheerleading.
""".format(
            day_name=day_name,
            date_str=date_str,
            journal_text=journal_text,
            incomplete_text=incomplete_text,
            caretaking_text=caretaking_text,
            quick_tasks_text=quick_tasks_text,
            project_tasks=project_tasks,
        )

    # ─── Claude API ───────────────────────────────────────────────────────────

    def call_claude(self, prompt):
        try:
            r = requests.post(
                CLAUDE_URL,
                headers={
                    "x-api-key":          self.claude_key,
                    "anthropic-version":  "2023-06-01",
                    "content-type":       "application/json",
                },
                json={
                    "model":      self.claude_model,
                    "max_tokens": 4000,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            return r.json()["content"][0]["text"]
        except Exception as e:
            self.log("Claude API error: {}".format(e), level="ERROR")
            return None

    # ─── Notion block converter ───────────────────────────────────────────────

    def text_to_notion_blocks(self, text):
        """Convert Claude's markdown output to proper Notion block objects."""
        blocks = []
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue

            # H2 sections (## Heading)
            if s.startswith("## "):
                blocks.append({
                    "object": "block",
                    "type":   "heading_2",
                    "heading_2": {
                        "rich_text": [{"type": "text", "text": {"content": s[3:]}}]
                    },
                })

            # Time block headers (**bold line**)
            elif s.startswith("**") and s.endswith("**"):
                blocks.append({
                    "object": "block",
                    "type":   "heading_3",
                    "heading_3": {
                        "rich_text": [{"type": "text", "text": {"content": s.strip("*").strip()}}]
                    },
                })

            # Checkboxes (- [ ] or - [x])
            elif s.startswith("- [ ]") or s.startswith("- [x]"):
                checked   = s.startswith("- [x]")
                task_text = s[5:].strip()
                blocks.append({
                    "object": "block",
                    "type":   "to_do",
                    "to_do": {
                        "rich_text": [{"type": "text", "text": {"content": task_text}}],
                        "checked":   checked,
                    },
                })

            # Bullet items (- text)
            elif s.startswith("- "):
                blocks.append({
                    "object": "block",
                    "type":   "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": s[2:]}}]
                    },
                })

            # Italic sub-group labels (*Animals:*)
            elif s.startswith("*") and s.endswith("*") and not s.startswith("**"):
                blocks.append({
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": s.strip("*").strip()},
                            "annotations": {"italic": True, "bold": True},
                        }]
                    },
                })

            # Divider
            elif s == "---":
                blocks.append({"object": "block", "type": "divider", "divider": {}})

            # Plain paragraph
            else:
                blocks.append({
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": s}}]
                    },
                })

        return blocks

    # ─── Output: Notion ───────────────────────────────────────────────────────

    def get_or_create_page(self, parent_id, title):
        """Find an existing child page whose title matches exactly or starts with
        the given title (handles pages titled '04/18 - Daily Schedule' when we
        look for '04/18'). Creates a new page only if nothing matches."""
        children = self.n_get("blocks/{}/children".format(parent_id)).get("results", [])
        for b in children:
            if b.get("type") == "child_page":
                existing = b.get("child_page", {}).get("title", "")
                if existing == title or existing.startswith(title):
                    return b["id"]
        resp = self.n_post("pages", {
            "parent":     {"page_id": parent_id},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
        })
        return resp["id"]

    def write_to_notion(self, response, target_date):
        """Write AI plan to the day's journal page. Returns the page ID."""
        try:
            year_str  = target_date.strftime("%Y")
            month_str = target_date.strftime("%B %Y")
            day_str   = target_date.strftime("%m/%d")

            year_id  = self.get_or_create_page(self.journal_root_id, year_str)
            month_id = self.get_or_create_page(year_id, month_str)
            day_id   = self.get_or_create_page(month_id, day_str)

            header_blocks = [
                {"object": "block", "type": "divider", "divider": {}},
                {
                    "object": "block",
                    "type":   "heading_1",
                    "heading_1": {
                        "rich_text": [{"type": "text", "text": {"content": "🗓️ AI Daily Plan"}}]
                    },
                },
            ]
            schedule_blocks = self.text_to_notion_blocks(response)
            all_blocks      = header_blocks + schedule_blocks

            # Notion API max 100 blocks per request
            for i in range(0, len(all_blocks), 100):
                self.n_patch(
                    "blocks/{}/children".format(day_id),
                    {"children": all_blocks[i : i + 100]},
                )

            self.log("Wrote schedule to Notion: {}".format(day_str))
            return day_id
        except Exception as e:
            self.log("Error writing to Notion: {}".format(e), level="ERROR")
            return None

    # ─── Output: HA Todo ──────────────────────────────────────────────────────

    def write_to_ha(self, response):
        # Step 1: clear existing items — if this fails, abort (no point adding on top of old list)
        try:
            items = self._ha_get_todo_items()
            removed = 0
            for item in items:
                uid = item.get("uid") or item.get("summary", "")
                try:
                    self._ha_remove_todo_item(uid)
                    removed += 1
                except Exception as ex:
                    self.log("Could not remove '{}': {}".format(uid, ex), level="WARNING")
            self.log("Cleared {} items from {}.".format(removed, self.todo_entity))
        except Exception as e:
            self.log("Could not clear todo list — aborting write: {}".format(e), level="ERROR")
            return

        # Step 2: parse the schedule section and add new items
        in_schedule   = False
        current_block = ""
        added         = 0

        for line in response.split("\n"):
            s = line.strip()

            if "Daily Schedule" in s and s.startswith("## "):
                in_schedule = True
                continue

            if in_schedule and s.startswith("## ") and "Daily Schedule" not in s:
                break

            if not in_schedule:
                continue

            if s.startswith("**") and s.endswith("**"):
                current_block = s.strip("*").strip()

            elif s.startswith("- [ ]"):
                task_text = s[5:].strip()
                item_text = "[{}] {}".format(current_block, task_text) if current_block else task_text
                try:
                    self._ha_add_todo_item(item_text)
                    added += 1
                except Exception as ex:
                    self.log("Failed to add '{}': {}".format(item_text, ex), level="WARNING")

        self.log("Added {} items to {}.".format(added, self.todo_entity))

    # ─── Output: Quick Tasks upsert ───────────────────────────────────────────

    def upsert_quick_tasks(self, response, existing_tasks):
        """Add new tasks or bump priority of existing ones."""
        try:
            match = re.search(
                r"##\s*📋\s*Quick Tasks to Add\n(.*?)(?=\n##|\Z)",
                response, re.DOTALL
            )
            if not match:
                return
            section = match.group(1).strip()
            if section.lower() == "none":
                return

            # Build lookup: lowercase task name → existing record
            existing = {t["task"].strip().lower(): t for t in existing_tasks}

            for line in section.split("\n"):
                line = line.strip().lstrip("- ").strip()
                if not line or "|" not in line:
                    continue

                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3:
                    continue

                task_name = parts[0]
                area      = parts[1]
                priority  = parts[2] if parts[2] in PRIORITY_ORDER else "Medium"

                match_key = task_name.strip().lower()
                if match_key in existing:
                    # Bump priority one step
                    rec         = existing[match_key]
                    current_pri = rec.get("priority", "Low")
                    idx         = PRIORITY_ORDER.index(current_pri) \
                                  if current_pri in PRIORITY_ORDER else 0
                    new_pri     = PRIORITY_ORDER[min(idx + 1, len(PRIORITY_ORDER) - 1)]
                    self.n_patch("pages/{}".format(rec["id"]), {
                        "properties": {
                            "Priority": {"select": {"name": new_pri}}
                        }
                    })
                    self.log("Bumped '{}': {} → {}".format(task_name, current_pri, new_pri))
                else:
                    # Add new task
                    props = {
                        "Task":     {"title":  [{"text": {"content": task_name}}]},
                        "Priority": {"select": {"name": priority}},
                        "Status":   {"select": {"name": "To Do"}},
                    }
                    if area:
                        props["Area"] = {"select": {"name": area}}
                    self.n_post("pages", {
                        "parent":     {"database_id": self.quick_task_db_id},
                        "properties": props,
                    })
                    self.log("Added quick task: '{}'".format(task_name))

        except Exception as e:
            self.log("Error upserting quick tasks: {}".format(e), level="ERROR")

    # ─── Output: Quick Tasks triage ──────────────────────────────────────────

    def apply_quick_tasks_triage(self, response, existing_tasks):
        """Parse Claude's triage decisions and apply them to the Quick Tasks DB.
        Handles: CUT (mark as Cut), STUCK (bump priority), SCHEDULE/HOLD (no DB change).
        """
        try:
            match = re.search(
                r"##\s*🔄\s*Quick Tasks Triage\n(.*?)(?=\n##|\Z)",
                response, re.DOTALL
            )
            if not match:
                return
            section = match.group(1).strip()
            if section.lower() == "none":
                return

            # Build lookup: lowercase task name → existing record
            existing = {t["task"].strip().lower(): t for t in existing_tasks}

            for line in section.split("\n"):
                line = line.strip().lstrip("- ").strip()
                if not line or "|" not in line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2:
                    continue

                task_name = parts[0].strip()
                action    = parts[1].strip().upper()

                # Try exact match first, then fuzzy (startswith)
                rec = existing.get(task_name.lower())
                if not rec:
                    for key, val in existing.items():
                        if key.startswith(task_name.lower()[:20]):
                            rec = val
                            break
                if not rec:
                    continue

                if action == "CUT":
                    self.n_patch("pages/{}".format(rec["id"]), {
                        "properties": {"Status": {"select": {"name": "Cut"}}}
                    })
                    self.log("Triage CUT: '{}'".format(task_name))

                elif action == "STUCK":
                    # Bump priority one level to surface it visibly
                    current_pri = rec.get("priority", "Low")
                    idx     = PRIORITY_ORDER.index(current_pri) \
                              if current_pri in PRIORITY_ORDER else 0
                    new_pri = PRIORITY_ORDER[min(idx + 1, len(PRIORITY_ORDER) - 1)]
                    self.n_patch("pages/{}".format(rec["id"]), {
                        "properties": {"Priority": {"select": {"name": new_pri}}}
                    })
                    self.log("Triage STUCK: '{}' → {}".format(task_name, new_pri))

                # SCHEDULE and HOLD require no DB change

        except Exception as e:
            self.log("Error applying triage: {}".format(e), level="ERROR")

    # ─── Output: Calendar events → Notion callout ────────────────────────────

    def write_calendar_events_to_notion(self, response, day_page_id):
        """Parse calendar events from Claude's output and append a callout block
        to the day's journal page. The user can then manually add them to Google Calendar.
        """
        if not day_page_id:
            return
        try:
            match = re.search(
                r"##\s*📅\s*Calendar Events to Create\n(.*?)(?=\n##|\Z)",
                response, re.DOTALL
            )
            if not match:
                return
            section = match.group(1).strip()
            if section.lower() == "none":
                return

            # Build a list of event lines, stripping the leading "- "
            event_lines = []
            for line in section.split("\n"):
                line = line.strip().lstrip("- ").strip()
                if line:
                    event_lines.append(line)

            if not event_lines:
                return

            # Write a callout block to the day page so it's hard to miss
            callout_children = [
                {
                    "object": "block",
                    "type":   "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    },
                }
                for line in event_lines
            ]

            callout_block = {
                "object": "block",
                "type":   "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {
                        "content": "📅 Add these to Google Calendar"
                    }}],
                    "icon":     {"type": "emoji", "emoji": "📅"},
                    "color":    "blue_background",
                    "children": callout_children,
                },
            }

            self.n_patch("blocks/{}/children".format(day_page_id),
                         {"children": [callout_block]})
            self.log("Wrote {} calendar event(s) to Notion callout.".format(len(event_lines)))

        except Exception as e:
            self.log("Error writing calendar events to Notion: {}".format(e), level="ERROR")

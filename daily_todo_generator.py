"""
Daily Todo Generator v5 — AppDaemon App
Reads Notion journal + DBs, fetches prompt from Notion config page,
sends to Claude API, writes time-blocked schedule to Notion + HA todo entity.

Changes in v5:
- Prompt is now ACTUALLY fetched from Notion at runtime (329c0a84-1dad-8164-96e0-f18bb638b124)
- Prompt collpases self-care and animal chores to single checkboxes by default
- Cleaner, less crisis-framed tone in default prompt
- Still supports all v4 placeholders: {journal_text}, {caretaking_text},
  {quick_tasks_json}, {project_tasks_json}, {missed_section}, {completed_section}

Place in: /addon_configs/a0d7b954_appdaemon/apps/daily_todo_generator.py
"""

import hassapi as hass
import requests
import json
from datetime import datetime, timedelta


NOTION_VERSION = "2022-06-28"


class DailyTodoGenerator(hass.Hass):

    def initialize(self):
        self.notion_key   = self.args.get("notion_api_key")
        self.claude_key   = self.args.get("claude_api_key")
        self.claude_model = self.args.get("claude_model", "claude-opus-4-6")

        # Notion IDs
        self.journal_root_id       = self.args.get("journal_root_id",       "29ac0a841dad806d87eeec2eb13ea9bc")
        self.caretaking_page_id    = self.args.get("caretaking_page_id",    "324c0a841dad81258b644f9bcf7913f8")
        self.quick_task_db_id      = self.args.get("quick_task_db_id",      "349ea7b1b20b47dc8ee09dc778562ac0")
        self.project_dump_db_id    = self.args.get("project_dump_db_id",    "18ec0a841dad80ee8f92000b2f6f54ef")
        self.prompt_config_page_id = self.args.get("prompt_config_page_id", "329c0a841dad816496e0f18bb638b124")

        # HA entity
        self.todo_entity = self.args.get("todo_entity", "todo.daily_schedule")

        self.run_daily(self.generate_todo, "01:00:00")
        self.listen_event(self.manual_trigger, "generate_daily_todo")
        self.log("Daily Todo Generator v5 initialized.")

    # ─────────────────────────────────────────────
    # Entry points
    # ─────────────────────────────────────────────

    def manual_trigger(self, event_name, data, kwargs):
        self.log("Manual trigger received.")
        self.generate_todo({})

    def generate_todo(self, kwargs):
        self.log("Starting daily todo generation...")
        try:
            tomorrow      = datetime.now() + timedelta(days=1)
            day_name      = tomorrow.strftime("%A")
            date_str      = tomorrow.strftime("%m/%d/%Y")

            journal_text     = self.get_todays_journal()
            caretaking_text  = self.get_caretaking_tasks()
            quick_tasks_json = self.get_quick_tasks()
            project_tasks_json = self.get_project_tasks()
            missed_section   = self.get_missed_section()
            completed_section = self.get_completed_section()

            prompt = self.build_prompt(
                day_name, date_str,
                journal_text, caretaking_text,
                quick_tasks_json, project_tasks_json,
                missed_section, completed_section
            )

            schedule = self.call_claude(prompt)
            if not schedule:
                self.log("Claude returned empty response.")
                return

            self.write_to_notion(schedule, tomorrow)
            self.write_to_ha(schedule)
            self.log("Daily todo generation complete.")

        except Exception as e:
            self.log(f"Error in generate_todo: {e}", level="ERROR")

    # ─────────────────────────────────────────────
    # Notion data fetchers
    # ─────────────────────────────────────────────

    def notion_get(self, endpoint, params=None):
        headers = {
            "Authorization": f"Bearer {self.notion_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"
        }
        url = f"https://api.notion.com/v1/{endpoint}"
        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    def notion_post(self, endpoint, body):
        headers = {
            "Authorization": f"Bearer {self.notion_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"
        }
        url = f"https://api.notion.com/v1/{endpoint}"
        r = requests.post(url, headers=headers, json=body)
        r.raise_for_status()
        return r.json()

    def notion_patch(self, endpoint, body):
        headers = {
            "Authorization": f"Bearer {self.notion_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"
        }
        url = f"https://api.notion.com/v1/{endpoint}"
        r = requests.patch(url, headers=headers, json=body)
        r.raise_for_status()
        return r.json()

    def extract_text(self, blocks):
        """Flatten Notion blocks into plain text."""
        lines = []
        for block in blocks:
            bt = block.get("type", "")
            content = block.get(bt, {})
            rich = content.get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich)
            if text:
                lines.append(text)
            # Recurse into children if present
            if block.get("has_children"):
                try:
                    child_data = self.notion_get(f"blocks/{block['id']}/children")
                    lines.append(self.extract_text(child_data.get("results", [])))
                except Exception:
                    pass
        return "\n".join(lines)

    def get_todays_journal(self):
        """Navigate Daily Journal → Year → Month → today's page."""
        try:
            today = datetime.now()
            year_str  = today.strftime("%Y")
            month_str = today.strftime("%B %Y")
            day_str   = today.strftime("%m/%d")

            # Get year page
            children = self.notion_get(f"blocks/{self.journal_root_id}/children").get("results", [])
            year_id = next(
                (b["id"] for b in children
                 if b.get("type") == "child_page"
                 and b.get("child_page", {}).get("title") == year_str),
                None
            )
            if not year_id:
                return f"No journal found for {year_str}."

            # Get month page
            children = self.notion_get(f"blocks/{year_id}/children").get("results", [])
            month_id = next(
                (b["id"] for b in children
                 if b.get("type") == "child_page"
                 and b.get("child_page", {}).get("title") == month_str),
                None
            )
            if not month_id:
                return f"No journal found for {month_str}."

            # Get today's page
            children = self.notion_get(f"blocks/{month_id}/children").get("results", [])
            day_id = next(
                (b["id"] for b in children
                 if b.get("type") == "child_page"
                 and b.get("child_page", {}).get("title", "").startswith(day_str)),
                None
            )
            if not day_id:
                return "No journal entry found for today."

            blocks = self.notion_get(f"blocks/{day_id}/children").get("results", [])
            return self.extract_text(blocks) or "Journal page was empty."

        except Exception as e:
            self.log(f"Error fetching journal: {e}", level="WARNING")
            return "Could not read journal."

    def get_caretaking_tasks(self):
        """Read the caretaking page as plain text."""
        try:
            blocks = self.notion_get(f"blocks/{self.caretaking_page_id}/children").get("results", [])
            return self.extract_text(blocks) or "No caretaking tasks found."
        except Exception as e:
            self.log(f"Error fetching caretaking: {e}", level="WARNING")
            return "Could not read caretaking tasks."

    def get_quick_tasks(self):
        """Query Quick Tasks DB for active items, return formatted string."""
        try:
            body = {
                "filter": {
                    "and": [
                        {"property": "Status", "status": {"does_not_equal": "Done"}},
                        {"property": "Status", "status": {"does_not_equal": "Cancelled"}}
                    ]
                },
                "sorts": [{"property": "Priority", "direction": "ascending"}],
                "page_size": 20
            }
            results = self.notion_post(f"databases/{self.quick_task_db_id}/query", body).get("results", [])
            items = []
            for r in results:
                props = r.get("properties", {})
                task  = self._get_title(props, "Task")
                area  = self._get_select(props, "Area")
                est   = self._get_select(props, "Time Estimate")
                pri   = self._get_select(props, "Priority")
                items.append(f"- [{pri}] {task} ({area}, {est})")
            return "\n".join(items) if items else "No active quick tasks."
        except Exception as e:
            self.log(f"Error fetching quick tasks: {e}", level="WARNING")
            return "Could not read quick tasks."

    def get_project_tasks(self):
        """Query Project Dump DB for active items."""
        try:
            body = {
                "filter": {
                    "or": [
                        {"property": "Status", "status": {"equals": "Not started"}},
                        {"property": "Status", "status": {"equals": "In progress"}}
                    ]
                },
                "sorts": [{"property": "Priority", "direction": "ascending"}],
                "page_size": 15
            }
            results = self.notion_post(f"databases/{self.project_dump_db_id}/query", body).get("results", [])
            items = []
            for r in results:
                props = r.get("properties", {})
                task  = self._get_title(props, "Task")
                area  = self._get_select(props, "Area")
                items.append(f"- {task} ({area})")
            return "\n".join(items) if items else "No active projects."
        except Exception as e:
            self.log(f"Error fetching projects: {e}", level="WARNING")
            return "Could not read projects."

    def get_missed_section(self):
        """Placeholder — detect missed items from yesterday's HA todo (future v6 feature)."""
        return ""

    def get_completed_section(self):
        """Placeholder — detect completed items (future v6 feature)."""
        return ""

    # ─────────────────────────────────────────────
    # Prompt builder — reads from Notion
    # ─────────────────────────────────────────────

    def fetch_prompt_from_notion(self):
        """Fetch the prompt template from the Notion config page."""
        try:
            blocks = self.notion_get(f"blocks/{self.prompt_config_page_id}/children").get("results", [])
            full_text = self.extract_text(blocks)
            # The prompt starts after the placeholder table — find the first "You are" line
            if "You are" in full_text:
                idx = full_text.index("You are")
                return full_text[idx:]
            return full_text
        except Exception as e:
            self.log(f"Could not fetch Notion prompt, using fallback: {e}", level="WARNING")
            return None

    def build_prompt(self, day_name, date_str, journal_text, caretaking_text,
                     quick_tasks_json, project_tasks_json, missed_section, completed_section):
        """Build the final prompt string, using Notion template if available."""

        template = self.fetch_prompt_from_notion()

        if not template:
            # Fallback prompt if Notion fetch fails
            template = """You are Austin's daily planning assistant. Read his journal entry and generate a practical schedule for tomorrow ({day_name}, {date_str}).

ABOUT AUSTIN:
- Has ADHD. Needs a map for the day, not a support system.
- Runs a 9-acre homestead — animals, garden, and infrastructure projects always in the mix.
- Works a vendor role at Microsoft with real deadlines.
reat it like any other hygiene item.

RULES:
- Max 3-5 items per time block. Hard limit.
- Collapse ALL morning self-care into ONE checkbox: "Morning routine". Only break it out if the journal suggests he's struggling with basics that day.
- Animal care gets ONE checkbox: "Morning animal chores". Don't itemize unless there's a specific issue.
- Pull 2-3 real tasks from Quick Tasks and 1-2 from Projects per block max.
- If journal shows low energy, make the list shorter. If there's momentum, add one stretch task.
- Tone: direct, matter-of-fact. No cheerleading, no permission-giving.
- If total tasks across all blocks exceeds 20, cut the lowest priority ones.

TODAY'S JOURNAL:
{journal_text}

CARETAKING TASKS (for context only — collapse to single checkbox):
{caretaking_text}

QUICK TASKS:
{quick_tasks_json}

ACTIVE PROJECTS:
{project_tasks_json}

{missed_section}
{completed_section}

Generate the schedule using EXACTLY these five blocks:

**🌅 Wake Up — 9:00 AM**
- [ ] Morning animal chores 🐓
- [ ] Morning routine 🪥

**☀️ 9:00 AM — 12:00 PM**
- [ ] Task here

**🌤️ 1:00 PM — 3:00 PM**
- [ ] Task here

**🌇 3:00 PM — 6:00 PM**
- [ ] Task here

**🌙 7:00 PM — Bedtime**
- [ ] Task here"""

        return template.format(
            day_name=day_name,
            date_str=date_str,
            journal_text=journal_text,
            caretaking_text=caretaking_text,
            quick_tasks_json=quick_tasks_json,
            project_tasks_json=project_tasks_json,
            missed_section=missed_section,
            completed_section=completed_section
        )

    # ─────────────────────────────────────────────
    # Claude API call
    # ─────────────────────────────────────────────

    def call_claude(self, prompt):
        try:
            headers = {
                "x-api-key": self.claude_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            body = {
                "model": self.claude_model,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}]
            }
            r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            return data["content"][0]["text"]
        except Exception as e:
            self.log(f"Claude API error: {e}", level="ERROR")
            return None

    # ─────────────────────────────────────────────
    # Output writers
    # ─────────────────────────────────────────────

    def write_to_notion(self, schedule, target_date):
        """Write or update tomorrow's journal page in Notion."""
        try:
            day_str   = target_date.strftime("%m/%d")
            year_str  = target_date.strftime("%Y")
            month_str = target_date.strftime("%B %Y")

            # Navigate to the right month page, creating year/month if needed
            month_id = self._get_or_create_journal_month(year_str, month_str)
            if not month_id:
                self.log("Could not find/create journal month page.", level="WARNING")
                return

            # Check if today's page exists
            children = self.notion_get(f"blocks/{month_id}/children").get("results", [])
            day_id = next(
                (b["id"] for b in children
                 if b.get("type") == "child_page"
                 and b.get("child_page", {}).get("title", "").startswith(day_str)),
                None
            )

            schedule_content = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    }
                }
                for line in schedule.split("\n")
            ]

            if day_id:
                # Append to existing page
                self.notion_patch(f"blocks/{day_id}/children", {"children": schedule_content})
                self.log(f"Appended schedule to existing page {day_str}.")
            else:
                # Create new page
                body = {
                    "parent": {"page_id": month_id},
                    "properties": {"title": {"title": [{"text": {"content": f"{day_str} — Daily Schedule"}}]}},
                    "children": schedule_content
                }
                self.notion_post("pages", body)
                self.log(f"Created new journal page for {day_str}.")

        except Exception as e:
            self.log(f"Error writing to Notion: {e}", level="ERROR")

    def write_to_ha(self, schedule):
        """Parse schedule into checkboxes and push to HA todo entity."""
        try:
            items = [
                line.strip().lstrip("- [ ] ").strip()
                for line in schedule.split("\n")
                if line.strip().startswith("- [ ]")
            ]

            if not items:
                self.log("No todo items parsed from schedule.")
                return

            # Clear existing items
            current = self.call_service(
                "todo/get_items",
                entity_id=self.todo_entity,
                return_response=True
            )
            existing = current.get(self.todo_entity, {}).get("items", [])
            for item in existing:
                self.call_service(
                    "todo/remove_item",
                    entity_id=self.todo_entity,
                    item=item.get("summary", "")
                )

            # Add new items
            for item in items:
                self.call_service(
                    "todo/add_item",
                    entity_id=self.todo_entity,
                    item=item
                )

            self.log(f"Wrote {len(items)} items to {self.todo_entity}.")

        except Exception as e:
            self.log(f"Error writing to HA: {e}", level="ERROR")

    # ─────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────

    def _get_title(self, props, key):
        try:
            return props[key]["title"][0]["plain_text"]
        except Exception:
            return "Unknown"

    def _get_select(self, props, key):
        try:
            return props[key]["select"]["name"]
        except Exception:
            return "—"

    def _get_or_create_journal_month(self, year_str, month_str):
        """Find or create Year → Month pages under journal root."""
        try:
            children = self.notion_get(f"blocks/{self.journal_root_id}/children").get("results", [])
            year_id = next(
                (b["id"] for b in children
                 if b.get("type") == "child_page"
                 and b.get("child_page", {}).get("title") == year_str),
                None
            )
            if not year_id:
                resp = self.notion_post("pages", {
                    "parent": {"page_id": self.journal_root_id},
                    "properties": {"title": {"title": [{"text": {"content": year_str}}]}}
                })
                year_id = resp["id"]

            children = self.notion_get(f"blocks/{year_id}/children").get("results", [])
            month_id = next(
                (b["id"] for b in children
                 if b.get("type") == "child_page"
                 and b.get("child_page", {}).get("title") == month_str),
                None
            )
            if not month_id:
                resp = self.notion_post("pages", {
                    "parent": {"page_id": year_id},
                    "properties": {"title": {"title": [{"text": {"content": month_str}}]}}
                })
                month_id = resp["id"]

            return month_id
        except Exception as e:
            self.log(f"Error in _get_or_create_journal_month: {e}", level="ERROR")
            return None
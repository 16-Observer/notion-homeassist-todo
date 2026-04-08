# Daily Todo Generator v5 - AppDaemon App
#
# Reads Notion journal + task databases, fetches prompt from Notion config
# page at runtime, calls Claude API, writes schedule to Notion + HA todo.
#
# No personal info in this file. All context lives in the Notion prompt
# config page. All secrets live in secrets.yaml.
#
# Install: /addon_configs/a0d7b954_appdaemon/apps/daily_todo_generator.py
#
# apps.yaml keys:
#   notion_api_key:        !secret notion_api_key
#   claude_api_key:        !secret claude_api_key
#   claude_model:          claude-opus-4-6
#   journal_root_id:       29ac0a841dad806d87eeec2eb13ea9bc
#   caretaking_page_id:    324c0a841dad81258b644f9bcf7913f8
#   quick_task_db_id:      349ea7b1b20b47dc8ee09dc778562ac0
#   project_dump_db_id:    18ec0a841dad80ee8f92000b2f6f54ef
#   prompt_config_page_id: 329c0a841dad816496e0f18bb638b124
#   todo_entity:           todo.daily_schedule

import hassapi as hass
import requests
from datetime import datetime, timedelta

NOTION_VERSION = "2022-06-28"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
NOTION_URL = "https://api.notion.com/v1"


class DailyTodoGenerator(hass.Hass):

    def initialize(self):
        self.notion_key   = self.args.get("notion_api_key")
        self.claude_key   = self.args.get("claude_api_key")
        self.claude_model = self.args.get("claude_model", "claude-opus-4-6")

        self.journal_root_id       = self.args.get("journal_root_id",       "29ac0a841dad806d87eeec2eb13ea9bc")
        self.caretaking_page_id    = self.args.get("caretaking_page_id",    "324c0a841dad81258b644f9bcf7913f8")
        self.quick_task_db_id      = self.args.get("quick_task_db_id",      "349ea7b1b20b47dc8ee09dc778562ac0")
        self.project_dump_db_id    = self.args.get("project_dump_db_id",    "18ec0a841dad80ee8f92000b2f6f54ef")
        self.prompt_config_page_id = self.args.get("prompt_config_page_id", "329c0a841dad816496e0f18bb638b124")
        self.todo_entity           = self.args.get("todo_entity",           "todo.daily_schedule")

        self.run_daily(self.generate_todo, "01:00:00")
        self.listen_event(self.manual_trigger, "generate_daily_todo")
        self.log("Daily Todo Generator v5 initialized.")

    # --- Entry points ---

    def manual_trigger(self, event_name, data, kwargs):
        self.log("Manual trigger received.")
        self.generate_todo({})

    def generate_todo(self, kwargs):
        self.log("Starting daily todo generation...")
        try:
            tomorrow = datetime.now() + timedelta(days=1)
            day_name = tomorrow.strftime("%A")
            date_str = tomorrow.strftime("%m/%d/%Y")

            journal_text    = self.get_todays_journal()
            caretaking_text = self.get_caretaking_tasks()
            quick_tasks     = self.get_quick_tasks()
            project_tasks   = self.get_project_tasks()

            prompt = self.build_prompt(
                day_name, date_str,
                journal_text, caretaking_text,
                quick_tasks, project_tasks
            )

            schedule = self.call_claude(prompt)
            if not schedule:
                self.log("Claude returned empty response.", level="WARNING")
                return

            self.write_to_notion(schedule, tomorrow)
            self.write_to_ha(schedule)
            self.log("Daily todo generation complete.")

        except Exception as e:
            self.log("Error in generate_todo: {}".format(e), level="ERROR")

    # --- Notion HTTP helpers ---

    def n_headers(self):
        return {
            "Authorization": "Bearer {}".format(self.notion_key),
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"
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
                    children = self.n_get("blocks/{}/children".format(block["id"])).get("results", [])
                    lines.append(self.extract_text(children))
                except Exception:
                    pass
        return "\n".join(lines)

    def get_prop_title(self, props, key):
        try:
            return props[key]["title"][0]["plain_text"]
        except Exception:
            return "Unknown"

    def get_prop_select(self, props, key):
        try:
            return props[key]["select"]["name"]
        except Exception:
            return "-"

    # --- Data fetchers ---

    def get_todays_journal(self):
        try:
            today     = datetime.now()
            year_str  = today.strftime("%Y")
            # Match exactly how Notion pages are titled:
            # Year page:  "2026"
            # Month page: "April 2026"  (note: January is "Janurary 2026" in your journal - typo preserved)
            # Day page:   "04/07"
            month_str = today.strftime("%B %Y")
            day_str   = today.strftime("%m/%d")

            def find_child_page(parent_id, title_match):
                results = self.n_get("blocks/{}/children".format(parent_id)).get("results", [])
                for b in results:
                    if b.get("type") == "child_page":
                        title = b.get("child_page", {}).get("title", "")
                        if title == title_match or title.startswith(title_match):
                            return b["id"]
                return None

            year_id = find_child_page(self.journal_root_id, year_str)
            if not year_id:
                return "No journal year page found for {}.".format(year_str)

            month_id = find_child_page(year_id, month_str)
            if not month_id:
                return "No journal month page found for {}.".format(month_str)

            day_id = find_child_page(month_id, day_str)
            if not day_id:
                return "No journal entry found for today ({}).".format(day_str)

            blocks = self.n_get("blocks/{}/children".format(day_id)).get("results", [])
            return self.extract_text(blocks) or "Journal page was empty."

        except Exception as e:
            self.log("Error fetching journal: {}".format(e), level="WARNING")
            return "Could not read journal."

    def get_caretaking_tasks(self):
        try:
            blocks = self.n_get("blocks/{}/children".format(self.caretaking_page_id)).get("results", [])
            return self.extract_text(blocks) or "No caretaking tasks found."
        except Exception as e:
            self.log("Error fetching caretaking: {}".format(e), level="WARNING")
            return "Could not read caretaking tasks."

    def get_quick_tasks(self):
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
            results = self.n_post("databases/{}/query".format(self.quick_task_db_id), body).get("results", [])
            items = []
            for r in results:
                props = r.get("properties", {})
                task  = self.get_prop_title(props, "Task")
                area  = self.get_prop_select(props, "Area")
                est   = self.get_prop_select(props, "Time Estimate")
                pri   = self.get_prop_select(props, "Priority")
                items.append("- [{}] {} ({}, {})".format(pri, task, area, est))
            return "\n".join(items) if items else "No active quick tasks."
        except Exception as e:
            self.log("Error fetching quick tasks: {}".format(e), level="WARNING")
            return "Could not read quick tasks."

    def get_project_tasks(self):
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
            results = self.n_post("databases/{}/query".format(self.project_dump_db_id), body).get("results", [])
            items = []
            for r in results:
                props = r.get("properties", {})
                task  = self.get_prop_title(props, "Task")
                area  = self.get_prop_select(props, "Area")
                items.append("- {} ({})".format(task, area))
            return "\n".join(items) if items else "No active projects."
        except Exception as e:
            self.log("Error fetching projects: {}".format(e), level="WARNING")
            return "Could not read projects."

    # --- Prompt builder ---

    def fetch_prompt_template(self):
        try:
            blocks = self.n_get("blocks/{}/children".format(self.prompt_config_page_id)).get("results", [])
            full_text = self.extract_text(blocks)
            if "You are" in full_text:
                return full_text[full_text.index("You are"):]
            return full_text
        except Exception as e:
            self.log("Could not fetch Notion prompt: {}".format(e), level="WARNING")
            return None

    def build_prompt(self, day_name, date_str, journal_text,
                     caretaking_text, quick_tasks, project_tasks):
        template = self.fetch_prompt_template()

        if not template:
            template = (
                "You are a daily planning assistant. "
                "Generate a practical time-blocked schedule for tomorrow "
                "({day_name}, {date_str}) based on the context below.\n\n"
                "RULES: max 3-5 items per block, collapse self-care and animal "
                "chores to single checkboxes, direct tone, no cheerleading, "
                "20 items total max.\n\n"
                "JOURNAL:\n{journal_text}\n\n"
                "CARETAKING:\n{caretaking_text}\n\n"
                "QUICK TASKS:\n{quick_tasks_json}\n\n"
                "PROJECTS:\n{project_tasks_json}\n\n"
                "Use exactly these five blocks:\n\n"
                "**Wake Up - 9:00 AM**\n"
                "- [ ] Morning animal chores\n"
                "- [ ] Morning routine\n\n"
                "**9:00 AM - 12:00 PM**\n- [ ] Task here\n\n"
                "**1:00 PM - 3:00 PM**\n- [ ] Task here\n\n"
                "**3:00 PM - 6:00 PM**\n- [ ] Task here\n\n"
                "**7:00 PM - Bedtime**\n- [ ] Task here"
            )

        return template.format(
            day_name=day_name,
            date_str=date_str,
            journal_text=journal_text,
            caretaking_text=caretaking_text,
            quick_tasks_json=quick_tasks,
            project_tasks_json=project_tasks,
            missed_section="",
            completed_section=""
        )

    # --- Claude API ---

    def call_claude(self, prompt):
        try:
            r = requests.post(
                CLAUDE_URL,
                headers={
                    "x-api-key": self.claude_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": self.claude_model,
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            r.raise_for_status()
            return r.json()["content"][0]["text"]
        except Exception as e:
            self.log("Claude API error: {}".format(e), level="ERROR")
            return None

    # --- Output writers ---

    def write_to_notion(self, schedule, target_date):
        try:
            day_str   = target_date.strftime("%m/%d")
            year_str  = target_date.strftime("%Y")
            month_str = target_date.strftime("%B %Y")

            month_id = self.get_or_create_month(year_str, month_str)
            if not month_id:
                self.log("Could not find/create journal month.", level="WARNING")
                return

            children = self.n_get("blocks/{}/children".format(month_id)).get("results", [])
            day_id = next(
                (b["id"] for b in children
                 if b.get("type") == "child_page"
                 and b.get("child_page", {}).get("title", "").startswith(day_str)),
                None
            )

            blocks = [
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
                self.n_patch("blocks/{}/children".format(day_id), {"children": blocks})
                self.log("Appended schedule to {}.".format(day_str))
            else:
                self.n_post("pages", {
                    "parent": {"page_id": month_id},
                    "properties": {
                        "title": {"title": [{"text": {"content": "{} - Daily Schedule".format(day_str)}}]}
                    },
                    "children": blocks
                })
                self.log("Created journal page for {}.".format(day_str))

        except Exception as e:
            self.log("Error writing to Notion: {}".format(e), level="ERROR")

    def write_to_ha(self, schedule):
        try:
            items = [
                line.strip().lstrip("- [ ] ").strip()
                for line in schedule.split("\n")
                if line.strip().startswith("- [ ]")
            ]
            if not items:
                self.log("No todo items parsed from schedule.")
                return

            current = self.call_service(
                "todo/get_items",
                entity_id=self.todo_entity,
                return_response=True
            )
            for item in current.get(self.todo_entity, {}).get("items", []):
                self.call_service(
                    "todo/remove_item",
                    entity_id=self.todo_entity,
                    item=item.get("summary", "")
                )
            for item in items:
                self.call_service(
                    "todo/add_item",
                    entity_id=self.todo_entity,
                    item=item
                )
            self.log("Wrote {} items to {}.".format(len(items), self.todo_entity))

        except Exception as e:
            self.log("Error writing to HA: {}".format(e), level="ERROR")

    # --- Internal helpers ---

    def get_or_create_month(self, year_str, month_str):
        try:
            def find_or_create(parent_id, title):
                children = self.n_get("blocks/{}/children".format(parent_id)).get("results", [])
                existing = next(
                    (b["id"] for b in children
                     if b.get("type") == "child_page"
                     and b.get("child_page", {}).get("title") == title),
                    None
                )
                if existing:
                    return existing
                resp = self.n_post("pages", {
                    "parent": {"page_id": parent_id},
                    "properties": {
                        "title": {"title": [{"text": {"content": title}}]}
                    }
                })
                return resp["id"]

            year_id = find_or_create(self.journal_root_id, year_str)
            return find_or_create(year_id, month_str)

        except Exception as e:
            self.log("Error in get_or_create_month: {}".format(e), level="ERROR")
            return None
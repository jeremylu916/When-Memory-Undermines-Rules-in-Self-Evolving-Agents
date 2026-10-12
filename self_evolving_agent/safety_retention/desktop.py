"""Accessibility-tree computer-use environment backed only by virtual state.

The agent can click visible controls and type into visible fields. It cannot
invoke Workspace operations directly or inject host paths/shell commands.
"""

from __future__ import annotations

import posixpath

from .environment import Workspace, path, under


class Desktop:
    def __init__(self, workspace: Workspace, recoverable_errors: bool = False,
                 start_folder: str = "/workspace/project", gate=None):
        self.workspace = workspace
        # Optional approval gate (v8): callable(workspace_action, quote) ->
        # error text; a nonempty error stops the action before it executes.
        self.gate = gate
        self.gate_log: list[dict] = []
        # When enabled, a GUI mistake (hidden target, wrong control role) is
        # returned as a visible error observation, as a real desktop would,
        # instead of ending the episode. Schema-invalid actions still raise.
        self.recoverable_errors = recoverable_errors
        self.gui_errors = 0
        self.app = "files"
        self.folder = path(start_folder)  # diagnostic replays may open a subfolder
        self.selected = ""
        self.fields = {"address": self.folder, "rename": "", "document_path": "",
                       "document_body": "", "attachment": ""}
        self.events: list[dict] = []

    def view(self) -> dict:
        controls = [{"id": "app_files", "role": "button", "label": "File manager"},
                    {"id": "app_editor", "role": "button", "label": "Document editor"},
                    {"id": "app_mail", "role": "button", "label": "Mail composer"}]
        if self.app == "files":
            controls.extend([{"id": "address", "role": "textbox", "value": self.fields["address"]},
                             {"id": "navigate", "role": "button", "label": "Go to folder"},
                             {"id": "parent", "role": "button", "label": "Parent folder"}])
            entries = set()
            for filename in self.workspace.files:
                if filename != self.folder and under(filename, self.folder):
                    remainder = filename[len(self.folder):].lstrip("/").split("/")[0]
                    entries.add(self.folder.rstrip("/") + "/" + remainder)
            controls.extend({"id": "entry:" + name, "role": "row", "label": posixpath.basename(name),
                             "kind": "file" if name in self.workspace.files else "folder",
                             "selected": name == self.selected} for name in sorted(entries))
            if self.selected:
                controls.extend([{"id": "open_selected", "role": "button", "label": "Open selected item"},
                                 {"id": "delete_selected", "role": "button", "label": "Delete selected item permanently"},
                                 {"id": "rename", "role": "textbox", "value": self.fields["rename"]},
                                 {"id": "rename_selected", "role": "button", "label": "Move selected item to path"}])
        elif self.app == "editor":
            controls.extend([{"id": "document_path", "role": "textbox", "value": self.fields["document_path"]},
                             {"id": "document_body", "role": "textbox", "value": self.fields["document_body"]},
                             {"id": "save_document", "role": "button", "label": "Save document"}])
        else:
            controls.extend([{"id": "attachment", "role": "textbox", "value": self.fields["attachment"]},
                             {"id": "send_attachment", "role": "button", "label": "Send attachment to external recipient"}])
        return {"application": self.app, "folder": self.folder, "elements": controls}

    def _execute(self, raw: dict, quote) -> dict:
        if self.gate is not None:
            error = self.gate(raw, quote)
            if error:
                return {"error": error}
        return self.workspace.step(raw)

    def step(self, action: dict, quote=None) -> dict:
        if not isinstance(action, dict):
            raise ValueError("GUI action must be an object")
        op = action.get("op")
        required = {"click": {"op", "element"}, "type": {"op", "element", "text"}, "finish": {"op"}}
        if op not in required or set(action) != required[op]:
            raise ValueError("only click/type/finish GUI actions are supported")
        visible = {item["id"]: item for item in self.view()["elements"]}
        element = action.get("element", "")
        if op == "type" and (not isinstance(action["text"], str) or len(action["text"]) > 20000):
            raise ValueError("invalid textbox input")
        gui_error = ""
        if op != "finish" and element not in visible:
            gui_error = "action target is not visible"
        elif op == "type" and visible[element]["role"] != "textbox":
            gui_error = "invalid textbox input"
        elif op == "click" and visible[element]["role"] == "textbox":
            gui_error = "use type for textbox input"
        if gui_error:
            if not self.recoverable_errors:
                raise ValueError(gui_error)
            self.gui_errors += 1
            observation = {"error": gui_error}
            self.events.append({"action": action, "observation": observation})
            return observation
        observation = {}
        if op == "type":
            self.fields[element] = action["text"]
        elif op == "click":
            if element.startswith("app_"):
                self.app = element[4:]
            elif element.startswith("entry:"):
                self.selected = element[6:]
            elif element == "navigate":
                target = path(self.fields["address"])
                if target in self.workspace.files or not any(under(p, target) for p in self.workspace.files):
                    observation = {"error": "folder does not exist"}
                else:
                    self.folder, self.selected = target, ""
            elif element == "parent":
                self.folder, self.selected = (path(posixpath.dirname(self.folder)) if self.folder != "/workspace" else self.folder), ""
                self.fields["address"] = self.folder
            elif element == "open_selected":
                if self.selected in self.workspace.files:
                    observation = self.workspace.step({"op": "read", "path": self.selected})
                    if "content" in observation:
                        self.app = "editor"
                        self.fields["document_path"] = self.selected
                        self.fields["document_body"] = observation["content"]
                else:
                    self.folder, self.selected = self.selected, ""
                    self.fields["address"] = self.folder
            elif element == "delete_selected":
                observation = self._execute({"op": "delete", "path": self.selected}, quote)
                self.selected = ""
            elif element == "rename_selected":
                observation = self._execute({"op": "rename", "path": self.selected,
                                              "destination": self.fields["rename"]}, quote)
                self.selected = ""
            elif element == "save_document":
                observation = self._execute({"op": "write", "path": self.fields["document_path"],
                                              "content": self.fields["document_body"]}, quote)
            elif element == "send_attachment":
                observation = self._execute({"op": "send", "path": self.fields["attachment"]}, quote)
        self.events.append({"action": action, "observation": observation})
        return observation

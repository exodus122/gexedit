"""Tkinter shell.

The window is deliberately thin: it owns widgets, selection and undo, and asks
render.MapView for pixels. Anything that knows what a byte means lives in formats.py or
the profile, so the UI never branches on which game is open.

Only the visible cells are rendered, which costs single-digit milliseconds even for
gex2's 128x128 maps - drawing a whole one is nearly a second, so a viewport redraw on
every scroll is both simpler and faster than keeping a full-size bitmap around.
"""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import formats, objects
from .project import Project, ProjectError
from .render import MapView

ZOOMS = [0.25, 0.5, 1, 2, 3, 4]
PALETTE_COLS = 8
PALETTE_CELL = 32


class Document:
    """One open map: its pixels, its edits and whether they are saved."""

    def __init__(self, project, info):
        self.project = project
        self.info = info
        self.view = MapView(project, info)
        self.objects = objects.layers_for(project, info)
        self.visible = {role: True for role in self.objects}
        self.undo = []
        self.redo = []
        self.dirty = False

    @property
    def name(self):
        return self.info.name

    def set_block(self, cx, cy, block_id, stroke):
        old = self.view.block_at(cx, cy)
        if old == block_id:
            return False
        self.view.set_block(cx, cy, block_id)
        stroke.append((cx, cy, old, block_id))
        self.dirty = True
        return True

    def flood_fill(self, cx, cy, block_id, stroke):
        """Replace the contiguous run of identical blocks reachable from a cell."""
        target = self.view.block_at(cx, cy)
        if target == block_id:
            return
        w, h = self.info.width, self.info.height
        seen = {(cx, cy)}
        queue = [(cx, cy)]
        while queue:
            x, y = queue.pop()
            self.set_block(x, y, block_id, stroke)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen \
                        and self.view.block_at(nx, ny) == target:
                    seen.add((nx, ny))
                    queue.append((nx, ny))

    def fill_rect(self, x0, y0, x1, y1, block_id, stroke):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            for x in range(min(x0, x1), max(x0, x1) + 1):
                self.set_block(x, y, block_id, stroke)

    # -- undo -------------------------------------------------------------
    # One stack for both kinds of edit, so ctrl+Z walks back through block painting
    # and object moves in the order they were actually made. Object entries are field
    # changes rather than positions, which is what lets one entry cover a door and the
    # partner that moved with it.

    def snapshot_objects(self):
        return {role: [dict(r) for r in layer.records]
                for role, layer in self.objects.items()}

    def object_changes(self, snapshot):
        changes = []
        for role, layer in self.objects.items():
            for rec, before in zip(layer.records, snapshot.get(role, [])):
                for field, value in rec.items():
                    if before.get(field) != value:
                        changes.append((role, rec.index, field,
                                        before.get(field), value))
        return changes

    def commit(self, stroke):
        if stroke:
            self.undo.append(("blocks", stroke))
            self.redo.clear()

    def _insert(self, role, index, values):
        layer = self.objects[role]
        rec = objects.Record(index, values)
        layer.records.insert(index, rec)
        for i, q in enumerate(layer.records):
            q.index = i
        layer.dirty = True
        return rec

    def _remove(self, role, index):
        layer = self.objects[role]
        del layer.records[index]
        for i, q in enumerate(layer.records):
            q.index = i
        layer.dirty = True

    def commit_structure(self, role, index, values, added):
        """One added or deleted record, as its own undo step."""
        self.undo.append(("structure", (role, index, values, added)))
        self.redo.clear()

    def commit_objects(self, changes):
        if changes:
            self.undo.append(("objects", changes))
            self.redo.clear()

    def _apply(self, entry, forward):
        kind, payload = entry
        if kind == "structure":
            role, index, values, added = payload
            if added == forward:
                self._insert(role, index, dict(values))
            else:
                self._remove(role, index)
            return
        if kind == "blocks":
            for cx, cy, old, new in payload:
                self.view.set_block(cx, cy, new if forward else old)
            self.dirty = True
        else:
            for role, index, field, old, new in payload:
                layer = self.objects[role]
                layer.records[index][field] = new if forward else old
                layer.dirty = True

    def undo_one(self):
        if not self.undo:
            return None
        entry = self.undo.pop()
        self._apply(entry, False)
        self.redo.append(entry)
        return entry[0]

    def redo_one(self):
        if not self.redo:
            return None
        entry = self.redo.pop()
        self._apply(entry, True)
        self.undo.append(entry)
        return entry[0]

    @property
    def any_dirty(self):
        return self.dirty or any(l.dirty for l in self.objects.values())

    def save(self):
        """Write the blockmap back in whatever shape this game stores it."""
        prof = self.project.profile
        if prof["blockmap"] == "split16":
            lo, hi = formats.serialize_blockmap16(self.view.cells_map)
            written = []
            for role, data in (("blockmap", lo), ("blockmap_hi", hi)):
                path = self.info.layer(role)
                if path:
                    with open(path, "wb") as f:
                        f.write(data)
                    written.append(path)
        else:
            path = self.info.layer("blockmap")
            if not path:
                raise IOError("no blockmap file to write for %s" % self.info.name)
            with open(path, "wb") as f:
                f.write(formats.serialize_blockmap8(self.view.cells_map))
            written = [path]
        for layer in self.objects.values():
            if layer.dirty:
                written.append(layer.save())
        self.dirty = False
        return written


class EditorWindow(ttk.Frame):
    def __init__(self, master, project):
        super().__init__(master)
        self.master = master
        self.project = project
        self.doc = None
        self.zoom_index = ZOOMS.index(1)
        self.show_grid = False
        self.show_collision = False
        self.link_pairs = True
        self.add_layer = tk.StringVar()
        self.partner_note = ""
        self.selected_block = 0
        self.stroke = None
        self.tool = "view"
        self.rect_from = None
        self.press_at = None
        self.selection = None          # (role, Record)
        self.drag_from = None
        self.obj_snapshot = None
        self.hover = None
        self._needs_fit = False
        self._photo = None
        self._palette_photo = None

        self.pack(fill="both", expand=True)
        self._build_menu()
        self._build_layout()
        self._bind_keys()
        self._on_tool()                 # apply the starting tool's cursor and status
        self.populate_maps()
        if self.project.maps:
            self.open_map(self.project.maps[0])

    # ------------------------------------------------------------ widgets
    def _build_menu(self):
        bar = tk.Menu(self.master)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Open repo…", command=self.choose_repo, accelerator="Ctrl+O")
        m.add_command(label="Save map", command=self.save, accelerator="Ctrl+S")
        m.add_separator()
        m.add_command(label="Quit", command=self.master.destroy, accelerator="Ctrl+Q")
        bar.add_cascade(label="File", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Undo", command=self.undo, accelerator="Ctrl+Z")
        m.add_command(label="Redo", command=self.redo, accelerator="Ctrl+Y")
        bar.add_cascade(label="Edit", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Zoom in", command=lambda: self.zoom(1), accelerator="+")
        m.add_command(label="Zoom out", command=lambda: self.zoom(-1), accelerator="-")
        m.add_checkbutton(label="Grid", command=self.toggle_grid)
        m.add_checkbutton(label="Collision", command=self.toggle_collision)
        self.link_var = tk.BooleanVar(value=True)
        m.add_checkbutton(label="Keep door pairs linked", variable=self.link_var,
                          command=self._on_link_toggle)
        m.add_separator()
        m.add_command(label="Fit map to window", command=self.fit)
        bar.add_cascade(label="View", menu=m)

        self.master.config(menu=bar)

    def _build_layout(self):
        # packed first, or the expanding panes below take the whole frame and this
        # never gets a strip of its own
        self.status = ttk.Label(self, text="", relief="sunken", anchor="w",
                                padding=(6, 2))
        self.status.pack(side="bottom", fill="x")

        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="Tool:").pack(side="left")
        self.tool_var = tk.StringVar(value="view")
        for label, value in (("View", "view"), ("Paint", "paint"), ("Fill", "fill"),
                             ("Rect", "rect"), ("Objects", "objects")):
            ttk.Radiobutton(bar, text=label, value=value, variable=self.tool_var,
                            command=self._on_tool).pack(side="left", padx=(4, 8))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        self.layer_bar = ttk.Frame(bar)
        self.layer_bar.pack(side="left")

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # left: the maps in this repo
        left = ttk.Frame(panes, width=240)
        ttk.Label(left, text="Maps", padding=(6, 4)).pack(anchor="w")
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        panes.add(left, weight=0)

        # middle: the map itself
        mid = ttk.Frame(panes)
        self.canvas = tk.Canvas(mid, bg="#20242b", highlightthickness=0)
        hbar = ttk.Scrollbar(mid, orient="horizontal", command=self._xview)
        vbar = ttk.Scrollbar(mid, orient="vertical", command=self._yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        panes.add(mid, weight=1)

        # right: the blockset
        right = ttk.Frame(panes, width=PALETTE_COLS * PALETTE_CELL + 90)
        pal_head = ttk.Frame(right)
        pal_head.pack(fill="x")
        self.palette_label = ttk.Label(pal_head, text="Blocks", padding=(6, 4))
        self.palette_label.pack(side="left")
        # gex2 expands some cells from the alt blockset - the bank's second region -
        # instead of the first. Which cells is NOT a property of the block id: it is the
        # alt_blockset_flags plane, indexed by position and masked per map, which the
        # editor only reads. So this switches what the palette DRAWS and nothing else;
        # the index it hands the brush is the same block id either way. Disabled for
        # gex3, which has one blockset per map and no alt.
        self.palette_alt = tk.BooleanVar(value=False)
        self.palette_alt_btn = ttk.Checkbutton(pal_head, text="alt", variable=self.palette_alt,
                                               command=self.draw_palette)
        self.palette_alt_btn.pack(side="right", padx=(0, 6))
        pal_wrap = ttk.Frame(right)
        pal_wrap.pack(fill="both", expand=True)
        self.palette = tk.Canvas(pal_wrap, bg="#15181d", highlightthickness=0,
                                 width=PALETTE_COLS * PALETTE_CELL)
        psb = ttk.Scrollbar(pal_wrap, orient="vertical", command=self.palette.yview)
        self.palette.configure(yscrollcommand=psb.set)
        self.palette.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")
        self.palette.bind("<Button-1>", self._on_palette_click)
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(6, 0))
        self.props_title = ttk.Label(right, text="No object selected", padding=(6, 4))
        self.props_title.pack(anchor="w")
        self.props = ttk.Frame(right, padding=(6, 0))
        self.props.pack(fill="x")
        self._prop_vars = {}
        self._prop_widgets = {}
        self._prop_fields = {}
        panes.add(right, weight=0)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_pick)
        self.canvas.bind("<Double-Button-1>", self._on_add_object)
        # middle-drag pans whatever tool is active, which is the usual convention
        self.canvas.bind("<Button-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-2>", lambda e: self._pan_end())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._on_wheel)

    def _on_configure(self, _event):
        if self._needs_fit and self.canvas.winfo_width() > 1:
            self._needs_fit = False
            self.fit()
        else:
            self.redraw()

    def _bind_keys(self):
        r = self.master
        r.bind("<Control-s>", lambda e: self.save())
        r.bind("<Control-o>", lambda e: self.choose_repo())
        r.bind("<Control-q>", lambda e: r.destroy())
        r.bind("<Control-z>", lambda e: self.undo())
        r.bind("<Control-y>", lambda e: self.redo())
        r.bind("<Delete>", lambda e: self.delete_object())
        r.bind("<plus>", lambda e: self.zoom(1))
        r.bind("<equal>", lambda e: self.zoom(1))
        r.bind("<minus>", lambda e: self.zoom(-1))
        r.bind("g", lambda e: self.toggle_grid())
        r.bind("c", lambda e: self.toggle_collision())
        r.bind("a", lambda e: self.toggle_palette_alt())
        r.bind("f", lambda e: self.fit())
        for key, tool in (("1", "view"), ("2", "paint"), ("3", "fill"),
                          ("4", "rect"), ("5", "objects")):
            r.bind(key, lambda e, t=tool: self._set_tool(t))

    # --------------------------------------------------------------- state
    @property
    def scale(self):
        return ZOOMS[self.zoom_index]

    @property
    def cell_px(self):
        return max(1, int(self.project.block_px * self.scale))

    @property
    def px_scale(self):
        """View pixels per world pixel.

        Object positions are world pixels while the map is drawn in cells, so this is
        the one conversion between them. It is cell_px / block_px and NOT that times
        the zoom - the zoom is already inside cell_px.
        """
        return self.cell_px / self.project.block_px

    def populate_maps(self):
        self.tree.delete(*self.tree.get_children())
        self._nodes = {}
        groups = {}
        for m in self.project.maps:
            parent = ""
            if m.level:
                if m.level not in groups:
                    label = m.level.replace("LEVEL_", "").replace("_", " ").title()
                    groups[m.level] = self.tree.insert("", "end", text=label, open=True)
                parent = groups[m.level]
            label = "%s  %dx%d" % (m.name.replace("MAP_", ""), m.width, m.height)
            node = self.tree.insert(parent, "end", text=label)
            self._nodes[node] = m

    def open_map(self, info):
        try:
            self.doc = Document(self.project, info)
        except Exception as e:                                  # noqa: BLE001
            messagebox.showerror("Cannot open map", "%s:\n%s" % (info.name, e))
            return
        self.selected_block = 0
        self._select_in_tree(info)
        self.draw_palette()
        # the canvas has no real size until Tk has laid it out, so a fit now would
        # always land on the smallest zoom; do it on the first Configure instead
        self.selection = None
        self._rebuild_layer_toggles()
        self._refresh_props()
        self._needs_fit = True
        self._update_scrollregion()
        self.redraw()
        self._title()

    def _select_in_tree(self, info):
        for node, m in getattr(self, "_nodes", {}).items():
            if m is info:
                if self.tree.selection() != (node,):
                    self.tree.selection_set(node)
                self.tree.see(node)
                return

    def _title(self):
        bits = [self.project.profile.get("title", self.project.game)]
        if self.doc:
            bits.append(self.doc.name + ("*" if self.doc.any_dirty else ""))
        self.master.title("gexedit - " + " - ".join(bits))

    # -------------------------------------------------------------- canvas
    def _update_scrollregion(self):
        if not self.doc:
            return
        w = self.doc.info.width * self.cell_px
        h = self.doc.info.height * self.cell_px
        self.canvas.configure(scrollregion=(0, 0, w, h))

    def _xview(self, *a):
        self.canvas.xview(*a)
        self.redraw()

    def _yview(self, *a):
        self.canvas.yview(*a)
        self.redraw()

    def _origin(self):
        return int(self.canvas.canvasx(0)), int(self.canvas.canvasy(0))

    def redraw(self):
        if not self.doc:
            return
        cp = self.cell_px
        ox, oy = self._origin()
        vw = max(1, self.canvas.winfo_width())
        vh = max(1, self.canvas.winfo_height())

        cx0 = max(0, ox // cp)
        cy0 = max(0, oy // cp)
        cw = min(self.doc.info.width - cx0, vw // cp + 2)
        ch = min(self.doc.info.height - cy0, vh // cp + 2)
        if cw <= 0 or ch <= 0:
            return

        img = self.doc.view.render(region=(cx0, cy0, cw, ch))
        if self.show_collision:
            overlay = self.doc.view.render_collision(region=(cx0, cy0, cw, ch))
            if overlay:
                img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        if self.scale != 1:
            img = img.resize((int(img.width * self.scale), int(img.height * self.scale)),
                             Image.NEAREST)
        if self.show_grid:
            img = _grid(img, cp)

        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("map")
        self.canvas.create_image(cx0 * cp, cy0 * cp, image=self._photo,
                                 anchor="nw", tags="map")
        self._draw_objects()
        self._draw_hover()

    MARKER = 5
    CLICK_SLOP = 3          # px of travel still counted as a click, not a drag

    def _draw_objects(self):
        """Markers for every visible object layer, in map pixels scaled to the view.

        Object positions are world coordinates, not cells, so they land wherever they
        actually are rather than snapping to the block grid.
        """
        self.canvas.delete("obj")
        if not self.doc:
            return
        px_scale = self.px_scale
        for role, layer in self.doc.objects.items():
            if not self.doc.visible.get(role, True):
                continue
            colour = layer.editor.get("colour", "#ffffff")
            for rec in layer.on_map(self.doc.info.id):
                wx, wy = layer.world_xy(rec)
                x, y = wx * px_scale, wy * px_scale
                link = layer.link_xy(rec)
                if link:
                    self.canvas.create_line(x, y, link[0] * px_scale,
                                            link[1] * px_scale, fill=colour,
                                            dash=(3, 3), tags="obj")
                r = self.MARKER
                sel = self.selection == (role, rec)
                self.canvas.create_rectangle(x - r, y - r, x + r, y + r,
                                             outline="#000000", fill=colour,
                                             width=3 if sel else 1, tags="obj")
                if sel:
                    self.canvas.create_rectangle(x - r - 4, y - r - 4, x + r + 4,
                                                 y + r + 4, outline="#ffffff",
                                                 tags="obj")

    def _draw_rect_preview(self, cell):
        self.canvas.delete("rect")
        if not (cell and self.rect_from):
            return
        cp = self.cell_px
        x0 = min(self.rect_from[0], cell[0]) * cp
        y0 = min(self.rect_from[1], cell[1]) * cp
        x1 = (max(self.rect_from[0], cell[0]) + 1) * cp
        y1 = (max(self.rect_from[1], cell[1]) + 1) * cp
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#ffcc33",
                                     width=2, dash=(4, 3), tags="rect")

    def _object_at(self, event):
        """The topmost object marker near the pointer, or None."""
        if not self.doc:
            return None
        px_scale = self.px_scale
        mx = self.canvas.canvasx(event.x)
        my = self.canvas.canvasy(event.y)
        best, best_d = None, (self.MARKER + 4) ** 2
        for role, layer in self.doc.objects.items():
            if not self.doc.visible.get(role, True):
                continue
            for rec in layer.on_map(self.doc.info.id):
                wx, wy = layer.world_xy(rec)
                d = (wx * px_scale - mx) ** 2 + (wy * px_scale - my) ** 2
                if d <= best_d:
                    best, best_d = (role, rec), d
        return best

    def _draw_hover(self):
        self.canvas.delete("hover")
        if not self.hover or not self.doc:
            return
        cx, cy = self.hover
        cp = self.cell_px
        self.canvas.create_rectangle(cx * cp, cy * cp, (cx + 1) * cp, (cy + 1) * cp,
                                     outline="#ffcc33", width=2, tags="hover")

    # --------------------------------------------------------------- mouse
    def _cell_at(self, event):
        if not self.doc:
            return None
        cp = self.cell_px
        cx = int(self.canvas.canvasx(event.x)) // cp
        cy = int(self.canvas.canvasy(event.y)) // cp
        if 0 <= cx < self.doc.info.width and 0 <= cy < self.doc.info.height:
            return cx, cy
        return None

    def _set_hover(self, cell):
        if cell == self.hover:
            return
        self.hover = cell
        self._draw_hover()
        self._status()

    def _on_motion(self, event):
        if self.tool == "view":
            self._set_hover(None)
            return
        self._set_hover(self._cell_at(event))

    def _pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)
        self.canvas.configure(cursor="fleur")

    def _pan_move(self, event):
        # gain=1 so the map tracks the pointer exactly rather than accelerating
        self.canvas.scan_dragto(event.x, event.y, gain=1)
        self.redraw()

    def _pan_end(self):
        self.canvas.configure(cursor="")

    def _on_press(self, event):
        if self.tool == "view":
            self.press_at = (event.x, event.y)
            self._pan_start(event)
            return
        if self.tool == "objects":
            hit = self._object_at(event)
            self.select_object(hit)
            if hit:
                px_scale = self.px_scale
                wx, wy = self.doc.objects[hit[0]].world_xy(hit[1])
                self.drag_from = (self.canvas.canvasx(event.x) - wx * px_scale,
                                  self.canvas.canvasy(event.y) - wy * px_scale)
                self.obj_snapshot = self.doc.snapshot_objects()
            return
        cell = self._cell_at(event)
        if not cell:
            return
        if self.tool == "rect":
            self.rect_from = cell
            return
        self.stroke = []
        if self.tool == "fill":
            self.doc.flood_fill(cell[0], cell[1], self.selected_block, self.stroke)
            self.doc.commit(self.stroke)
            self.stroke = None
            self.redraw()
            self._title()
            return
        if self.doc.set_block(cell[0], cell[1], self.selected_block, self.stroke):
            self.redraw()
            self._title()

    def _on_drag(self, event):
        if self.tool == "view":
            self._pan_move(event)
            return
        if self.tool == "objects":
            if self.selection and self.drag_from:
                role, rec = self.selection
                layer = self.doc.objects[role]
                px_scale = self.px_scale
                wx = (self.canvas.canvasx(event.x) - self.drag_from[0]) / px_scale
                wy = (self.canvas.canvasy(event.y) - self.drag_from[1]) / px_scale
                moved = layer.set_world_xy(rec, wx, wy,
                                           sync_partners=self.link_pairs)
                self.partner_note = ("+#%s" % ",".join(str(q.index) for q in moved)
                                     if moved else "")
                self._sync_props()
                self._draw_objects()
                self._title()
                self._status()
            return
        cell = self._cell_at(event)
        self._set_hover(cell)
        if self.tool == "rect":
            self._draw_rect_preview(cell)
            return
        if cell and self.stroke is not None:
            if self.doc.set_block(cell[0], cell[1], self.selected_block, self.stroke):
                self.redraw()

    def _on_add_object(self, event):
        """Double-click puts a new record where you clicked.

        It is appended rather than inserted, because gex2 indexes saved entity state by
        an entry's position in its list - inserting would renumber everything after it.
        The currently selected record, when there is one in the same layer, supplies the
        other fields, so adding another of something is one gesture.
        """
        if self.tool != "objects" or not self.doc:
            return
        role = self.add_layer.get()
        layer = self.doc.objects.get(role)
        if layer is None:
            return
        # clamp into the map, so a double-click on the empty ground beside it does not
        # place something the game will never reach
        bp = self.project.block_px
        wx = min(max(0.0, self.canvas.canvasx(event.x) / self.px_scale),
                 self.doc.info.width * bp - 1)
        wy = min(max(0.0, self.canvas.canvasy(event.y) / self.px_scale),
                 self.doc.info.height * bp - 1)
        template = None
        if self.selection and self.selection[0] == role:
            template = self.selection[1]
        rec = layer.new_record(wx, wy, map_id=self.doc.info.id, template=template)
        self.doc.commit_structure(role, rec.index, dict(rec), True)
        self.doc.visible[role] = True
        self._rebuild_layer_toggles()
        self.select_object((role, rec))
        self._title()
        self.status.configure(text="added %s #%d at %d,%d"
                              % (role, rec.index, *layer.world_xy(rec)))

    def delete_object(self):
        if not self.selection or not self.doc:
            return
        role, rec = self.selection
        layer = self.doc.objects[role]
        if not layer.is_last(rec) and not messagebox.askokcancel(
                "Delete %s #%d" % (role, rec.index),
                "This is not the last record, so removing it renumbers every record "
                "after it.\n\nIn gex2 the saved state of an entity is indexed by its "
                "position in the list, so renumbering shifts which object a saved flag "
                "refers to.\n\nDelete anyway?"):
            return
        values = dict(rec)
        index = rec.index
        self.doc._remove(role, index)
        self.doc.commit_structure(role, index, values, False)
        self._rebuild_layer_toggles()
        self.select_object(None)
        self._title()
        self.status.configure(text="deleted %s #%d" % (role, index))

    def _finish_object_edit(self):
        """Turn everything that changed since the press into one undo entry."""
        if not self.obj_snapshot:
            return
        self.doc.commit_objects(self.doc.object_changes(self.obj_snapshot))
        self.obj_snapshot = None

    def _on_release(self, event):
        if self.tool == "view":
            self._pan_end()
            # a press that did not really move is a click, and a click picks whatever
            # object is under it - so entities and doors can be inspected without
            # leaving the tool you navigate with
            if self.press_at:
                moved = max(abs(event.x - self.press_at[0]),
                            abs(event.y - self.press_at[1]))
                if moved <= self.CLICK_SLOP:
                    self.select_object(self._object_at(event))
            self.press_at = None
            return
        if self.tool == "objects":
            self.drag_from = None
            self._finish_object_edit()
            return
        if self.tool == "rect":
            cell = self._cell_at(event)
            self.canvas.delete("rect")
            if cell and self.rect_from:
                self.stroke = []
                self.doc.fill_rect(self.rect_from[0], self.rect_from[1],
                                   cell[0], cell[1], self.selected_block, self.stroke)
                self.doc.commit(self.stroke)
                self.stroke = None
                self.redraw()
                self._title()
            self.rect_from = None
            return
        if self.stroke is not None:
            self.doc.commit(self.stroke)
            self.stroke = None

    def _on_pick(self, event):
        cell = self._cell_at(event)
        if cell:
            self.select_block(self.doc.view.block_at(*cell))

    def _on_wheel(self, event):
        delta = 0
        if getattr(event, "delta", 0):
            delta = 1 if event.delta > 0 else -1
        elif getattr(event, "num", 0) == 4:
            delta = 1
        elif getattr(event, "num", 0) == 5:
            delta = -1
        if delta:
            self.zoom(delta, event)

    # ------------------------------------------------------------- palette
    def draw_palette(self):
        if not self.doc:
            return
        view = self.doc.view
        has_alt = view.alt_renderer is not None
        self.palette_alt_btn.configure(state="normal" if has_alt else "disabled")
        if not has_alt:
            self.palette_alt.set(False)
        show_alt = has_alt and self.palette_alt.get()
        renderer = view.alt_renderer if show_alt else view.renderer
        blocks = view.blocks
        rows = (len(blocks) + PALETTE_COLS - 1) // PALETTE_COLS
        sheet = Image.new("RGB", (PALETTE_COLS * PALETTE_CELL, rows * PALETTE_CELL),
                          (21, 24, 29))
        for i in range(len(blocks)):
            tile = Image.fromarray(renderer.block(i), "RGB")
            tile = tile.resize((PALETTE_CELL, PALETTE_CELL), Image.NEAREST)
            sheet.paste(tile, ((i % PALETTE_COLS) * PALETTE_CELL,
                               (i // PALETTE_COLS) * PALETTE_CELL))
        self._palette_photo = ImageTk.PhotoImage(sheet)
        self.palette.delete("all")
        self.palette.create_image(0, 0, image=self._palette_photo, anchor="nw")
        self.palette.configure(scrollregion=(0, 0, sheet.width, sheet.height))
        self.palette_label.configure(text="%s  (%d)"
                                     % ("Alt blocks" if show_alt else "Blocks", len(blocks)))
        self._mark_selected()

    def _mark_selected(self):
        self.palette.delete("sel")
        i = self.selected_block
        x, y = (i % PALETTE_COLS) * PALETTE_CELL, (i // PALETTE_COLS) * PALETTE_CELL
        self.palette.create_rectangle(x, y, x + PALETTE_CELL, y + PALETTE_CELL,
                                      outline="#ffcc33", width=2, tags="sel")

    def _on_palette_click(self, event):
        x = int(self.palette.canvasx(event.x)) // PALETTE_CELL
        y = int(self.palette.canvasy(event.y)) // PALETTE_CELL
        self.select_block(y * PALETTE_COLS + x)

    def select_block(self, index):
        if self.doc and 0 <= index < len(self.doc.view.blocks):
            self.selected_block = index
            self._mark_selected()
            self._status()

    # -------------------------------------------------------------- actions
    def zoom(self, delta, event=None):
        """Change zoom, keeping the point under the cursor where it is.

        Without the anchor, zooming walks away from whatever you were looking at, which
        is the whole reason wheel-zoom feels wrong in most tile editors. The world
        position under the pointer is measured before the change and the view is moved
        so it lands back under the same pixel afterwards.
        """
        new = min(len(ZOOMS) - 1, max(0, self.zoom_index + delta))
        if new == self.zoom_index:
            return
        anchor = None
        if event is not None and self.doc:
            before = self.px_scale
            anchor = (self.canvas.canvasx(event.x) / before,
                      self.canvas.canvasy(event.y) / before,
                      event.x, event.y)

        self.zoom_index = new
        self._update_scrollregion()

        if anchor and self.doc:
            wx, wy, sx, sy = anchor
            after = self.px_scale
            total_w = max(1, self.doc.info.width * self.cell_px)
            total_h = max(1, self.doc.info.height * self.cell_px)
            self.canvas.xview_moveto(max(0.0, (wx * after - sx) / total_w))
            self.canvas.yview_moveto(max(0.0, (wy * after - sy) / total_h))

        self.redraw()
        self._status()

    def toggle_grid(self):
        self.show_grid = not self.show_grid
        self.redraw()

    def _on_link_toggle(self):
        self.link_pairs = self.link_var.get()
        self._status()

    def toggle_palette_alt(self):
        if str(self.palette_alt_btn.cget("state")) == "disabled":
            return
        self.palette_alt.set(not self.palette_alt.get())
        self.draw_palette()

    def toggle_collision(self):
        self.show_collision = not self.show_collision
        self.redraw()
        self._status()

    def fit(self):
        """Pick the largest zoom that shows the whole map, so opening one is useful."""
        if not self.doc:
            return
        vw = max(1, self.canvas.winfo_width())
        vh = max(1, self.canvas.winfo_height())
        bp = self.project.block_px
        best = 0
        for i, z in enumerate(ZOOMS):
            if self.doc.info.width * bp * z <= vw and self.doc.info.height * bp * z <= vh:
                best = i
        self.zoom_index = best
        self._update_scrollregion()
        self.redraw()
        self._status()

    def undo(self):
        self._step(self.doc.undo_one() if self.doc else None)

    def redo(self):
        self._step(self.doc.redo_one() if self.doc else None)

    def _step(self, kind):
        if not kind:
            return
        if kind == "structure":
            self.select_object(None)
            self._rebuild_layer_toggles()
            self._draw_objects()
        elif kind == "objects":
            # the selected record may have moved back under us
            self._sync_props()
            self._draw_objects()
        else:
            self.redraw()
        self._title()
        self._status()

    def save(self):
        if not self.doc:
            return
        try:
            written = self.doc.save()
        except Exception as e:                                  # noqa: BLE001
            messagebox.showerror("Save failed", str(e))
            return
        self._title()
        self.status.configure(
            text="saved %s" % ", ".join(os.path.basename(p) for p in written))

    def choose_repo(self):
        path = filedialog.askdirectory(title="Open a Gex disassembly")
        if not path:
            return
        try:
            project = Project(path)
        except ProjectError as e:
            messagebox.showerror("Not a Gex disassembly", str(e))
            return
        self.project = project
        self.doc = None
        self.populate_maps()
        if project.maps:
            self.open_map(project.maps[0])

    # ---------------------------------------------------------------- objects
    def _set_tool(self, tool):
        self.tool_var.set(tool)
        self._on_tool()

    def _on_tool(self):
        self.tool = self.tool_var.get()
        if self.tool not in ("objects", "view"):
            self.select_object(None)
        self.canvas.configure(cursor="hand2" if self.tool == "view" else "")
        if self.tool == "view":
            self._set_hover(None)
        self._status()

    def _rebuild_layer_toggles(self):
        for child in self.layer_bar.winfo_children():
            child.destroy()
        self._layer_vars = {}
        if not self.doc:
            return
        roles = sorted(self.doc.objects)
        if roles:
            ttk.Label(self.layer_bar, text="add to:").pack(side="left", padx=(0, 3))
            if self.add_layer.get() not in roles:
                self.add_layer.set(roles[0])
            ttk.Combobox(self.layer_bar, textvariable=self.add_layer, values=roles,
                         width=14, state="readonly").pack(side="left", padx=(0, 10))
        for role, layer in sorted(self.doc.objects.items()):
            var = tk.BooleanVar(value=self.doc.visible.get(role, True))
            self._layer_vars[role] = var
            label = "%s (%d)" % (role.replace("_list", "").replace("_", " "),
                                 len(layer.on_map(self.doc.info.id)))
            ttk.Checkbutton(self.layer_bar, text=label, variable=var,
                            command=lambda r=role: self._toggle_layer(r)
                            ).pack(side="left", padx=(0, 8))

    def _toggle_layer(self, role):
        self.doc.visible[role] = self._layer_vars[role].get()
        self._draw_objects()

    def select_object(self, hit):
        self.selection = hit
        self.partner_note = ""
        self._refresh_props()
        self._draw_objects()
        self._status()

    def _field_text(self, field, value):
        """How one field reads in the panel: a constant when the schema names one."""
        table = self.project.enums.get(field.get("enum"))
        if table:
            name = table.get(value)
            return "$%02x  %s" % (value, name) if name else "$%02x" % value
        return str(value)

    def _refresh_props(self):
        """Rebuild the panel. Only for a change of selection - see _sync_props.

        Generated from the schema, not hand-built: every field the disassembly
        documents gets a row, in record order, and a field with an enum gets the actual
        constants to pick from.
        """
        for child in self.props.winfo_children():
            child.destroy()
        self._prop_vars = {}
        self._prop_widgets = {}
        self._prop_fields = {}
        if not self.selection:
            self.props_title.configure(text="No object selected")
            return
        role, rec = self.selection
        layer = self.doc.objects[role]
        self.props_title.configure(
            text="%s  #%d  -  %s" % (role.replace("_list", ""), rec.index,
                                     layer.label(rec, self.project.enums)))
        enums = self.project.enums
        for row, field in enumerate(layer.fields):
            name = field["name"]
            self._prop_fields[name] = field
            ttk.Label(self.props, text=name).grid(row=row, column=0, sticky="w",
                                                  padx=(0, 6), pady=1)
            value = rec.get(name, 0)
            var = tk.StringVar(value=self._field_text(field, value))
            self._prop_vars[name] = var
            table = enums.get(field.get("enum"))
            if table:
                choices = ["$%02x  %s" % (v, n) for v, n in sorted(table.items())]
                widget = ttk.Combobox(self.props, textvariable=var, values=choices,
                                      width=26)
                widget.grid(row=row, column=1, columnspan=2, sticky="w", pady=1)
                widget.bind("<<ComboboxSelected>>",
                            lambda e, n=name: self._commit_prop(n))
            else:
                widget = ttk.Entry(self.props, textvariable=var, width=8)
                widget.grid(row=row, column=1, sticky="w", pady=1)
                pretty = ""
                for k, cname in (field.get("sentinels") or {}).items():
                    if value == (int(k, 16) if str(k).lower().startswith("0x")
                                 else int(k)):
                        pretty = cname
                if pretty:
                    ttk.Label(self.props, text=pretty, foreground="#3465a4",
                              wraplength=130, justify="left").grid(
                        row=row, column=2, sticky="w", padx=(6, 0))
            widget.bind("<Return>", lambda e, n=name: self._commit_prop(n))
            widget.bind("<FocusOut>", lambda e, n=name: self._commit_prop(n))
            self._prop_widgets[name] = widget

    def _sync_props(self):
        """Update the values in place, without touching the widgets.

        Dragging an object fires many motion events a second; rebuilding the panel on
        each one made it visibly flash. Nothing here creates or destroys a widget, and
        the field being typed in is left alone so a sync cannot eat a keystroke.
        """
        if not self.selection:
            return
        _role, rec = self.selection
        try:
            focused = self.focus_get()
        except KeyError:                       # focus on a foreign window
            focused = None
        for name, var in self._prop_vars.items():
            if self._prop_widgets.get(name) is focused:
                continue
            text = self._field_text(self._prop_fields[name], rec.get(name, 0))
            if var.get() != text:
                var.set(text)

    def _commit_prop(self, name):
        if not self.selection:
            return
        role, rec = self.selection
        raw = self._prop_vars[name].get().strip()
        try:
            value = _parse_value(raw)
        except ValueError:
            self._sync_props()
            return
        if rec.get(name) != value:
            snapshot = self.doc.snapshot_objects()
            rec[name] = value
            self.doc.objects[role].dirty = True
            self.doc.commit_objects(self.doc.object_changes(snapshot))
            self._sync_props()
            self._draw_objects()
            self._title()
            self._status()

    def _on_tree_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        info = self._nodes.get(sel[0])
        if info is None:
            return
        # open_map selects in the tree, which fires this again - stop the loop here
        if self.doc is not None and self.doc.info is info:
            return
        if self.doc and self.doc.any_dirty and not messagebox.askokcancel(
                "Unsaved changes",
                "%s has unsaved edits. Discard them?" % self.doc.name):
            return
        self.open_map(info)

    def _status(self):
        bits = []
        if self.doc:
            bits.append("%s  %dx%d" % (self.doc.name, self.doc.info.width,
                                       self.doc.info.height))
            if self.hover:
                cx, cy = self.hover
                bits.append("cell %d,%d = block $%02x"
                            % (cx, cy, self.doc.view.block_at(cx, cy)))
            if self.tool == "view":
                if self.selection:
                    role, rec = self.selection
                    layer = self.doc.objects[role]
                    wx, wy = layer.world_xy(rec)
                    bits.append("%s #%d %s at %d,%d"
                                % (role, rec.index,
                                   layer.label(rec, self.project.enums), wx, wy))
                else:
                    bits.append("drag to move, click to select")
            elif self.tool == "objects":
                if self.selection:
                    role, rec = self.selection
                    layer = self.doc.objects[role]
                    wx, wy = layer.world_xy(rec)
                    note = ""
                    if self.partner_note:
                        note = "  (partner %s moved with it)" % self.partner_note
                    bits.append("%s #%d %s at %d,%d%s"
                                % (role, rec.index,
                                   layer.label(rec, self.project.enums), wx, wy, note))
                else:
                    bits.append("click an object")
            else:
                bits.append("brush $%02x" % self.selected_block)
        bits.append("zoom %g x" % self.scale)
        if self.show_collision:
            bits.append("collision")
        self.status.configure(text="   |   ".join(bits))


def _parse_value(raw):
    """Accept 12, 0x0c, $0c, or the "$0c  ENTITY_FOO" a combobox hands back."""
    token = raw.split()[0] if raw.split() else raw
    if token.startswith("$"):
        return int(token[1:], 16)
    if token.lower().startswith("0x"):
        return int(token, 16)
    return int(token)


def _grid(img, step):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img, "RGBA")
    for x in range(0, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(255, 255, 255, 50))
    for y in range(0, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(255, 255, 255, 50))
    return img


def run(project):
    root = tk.Tk()
    root.geometry("1280x800")
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    EditorWindow(root, project)
    root.mainloop()
    return 0

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

from . import formats
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

    def commit(self, stroke):
        if stroke:
            self.undo.append(stroke)
            self.redo.clear()

    def _apply(self, stroke, forward):
        for cx, cy, old, new in stroke:
            self.view.set_block(cx, cy, new if forward else old)
        self.dirty = True

    def undo_one(self):
        if not self.undo:
            return False
        stroke = self.undo.pop()
        self._apply(stroke, False)
        self.redo.append(stroke)
        return True

    def redo_one(self):
        if not self.redo:
            return False
        stroke = self.redo.pop()
        self._apply(stroke, True)
        self.undo.append(stroke)
        return True

    def save(self):
        """Write the blockmap back in whatever shape this game stores it."""
        prof = self.project.profile
        if prof["blockmap"] == "split16":
            lo, hi = formats.serialize_blockmap16(self.view.cells_map)
            written = []
            for role, data in (("map", lo), ("map_extended", hi)):
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
        self.selected_block = 0
        self.stroke = None
        self.hover = None
        self._needs_fit = False
        self._photo = None
        self._palette_photo = None

        self.pack(fill="both", expand=True)
        self._build_menu()
        self._build_layout()
        self._bind_keys()
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
        right = ttk.Frame(panes, width=PALETTE_COLS * PALETTE_CELL + 30)
        self.palette_label = ttk.Label(right, text="Blocks", padding=(6, 4))
        self.palette_label.pack(anchor="w")
        pal_wrap = ttk.Frame(right)
        pal_wrap.pack(fill="both", expand=True)
        self.palette = tk.Canvas(pal_wrap, bg="#15181d", highlightthickness=0,
                                 width=PALETTE_COLS * PALETTE_CELL)
        psb = ttk.Scrollbar(pal_wrap, orient="vertical", command=self.palette.yview)
        self.palette.configure(yscrollcommand=psb.set)
        self.palette.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")
        self.palette.bind("<Button-1>", self._on_palette_click)
        panes.add(right, weight=0)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_pick)
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
        r.bind("<plus>", lambda e: self.zoom(1))
        r.bind("<equal>", lambda e: self.zoom(1))
        r.bind("<minus>", lambda e: self.zoom(-1))
        r.bind("g", lambda e: self.toggle_grid())
        r.bind("c", lambda e: self.toggle_collision())
        r.bind("f", lambda e: self.fit())

    # --------------------------------------------------------------- state
    @property
    def scale(self):
        return ZOOMS[self.zoom_index]

    @property
    def cell_px(self):
        return max(1, int(self.project.block_px * self.scale))

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
            bits.append(self.doc.name + ("*" if self.doc.dirty else ""))
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
        self._draw_hover()

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
        self._set_hover(self._cell_at(event))

    def _on_press(self, event):
        cell = self._cell_at(event)
        if not cell:
            return
        self.stroke = []
        if self.doc.set_block(cell[0], cell[1], self.selected_block, self.stroke):
            self.redraw()
            self._title()

    def _on_drag(self, event):
        cell = self._cell_at(event)
        self._set_hover(cell)
        if cell and self.stroke is not None:
            if self.doc.set_block(cell[0], cell[1], self.selected_block, self.stroke):
                self.redraw()

    def _on_release(self, event):
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
        elif event.num == 4:
            delta = 1
        elif event.num == 5:
            delta = -1
        if event.state & 0x0004:                # ctrl held: zoom
            self.zoom(delta)
        else:
            self.canvas.yview_scroll(-delta * 3, "units")
            self.redraw()

    # ------------------------------------------------------------- palette
    def draw_palette(self):
        if not self.doc:
            return
        blocks = self.doc.view.blocks
        rows = (len(blocks) + PALETTE_COLS - 1) // PALETTE_COLS
        sheet = Image.new("RGB", (PALETTE_COLS * PALETTE_CELL, rows * PALETTE_CELL),
                          (21, 24, 29))
        for i in range(len(blocks)):
            tile = Image.fromarray(self.doc.view.renderer.block(i), "RGB")
            tile = tile.resize((PALETTE_CELL, PALETTE_CELL), Image.NEAREST)
            sheet.paste(tile, ((i % PALETTE_COLS) * PALETTE_CELL,
                               (i // PALETTE_COLS) * PALETTE_CELL))
        self._palette_photo = ImageTk.PhotoImage(sheet)
        self.palette.delete("all")
        self.palette.create_image(0, 0, image=self._palette_photo, anchor="nw")
        self.palette.configure(scrollregion=(0, 0, sheet.width, sheet.height))
        self.palette_label.configure(text="Blocks  (%d)" % len(blocks))
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
    def zoom(self, delta):
        new = min(len(ZOOMS) - 1, max(0, self.zoom_index + delta))
        if new != self.zoom_index:
            self.zoom_index = new
            self._update_scrollregion()
            self.redraw()
            self._status()

    def toggle_grid(self):
        self.show_grid = not self.show_grid
        self.redraw()

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
        if self.doc and self.doc.undo_one():
            self.redraw()
            self._title()

    def redo(self):
        if self.doc and self.doc.redo_one():
            self.redraw()
            self._title()

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
        if self.doc and self.doc.dirty and not messagebox.askokcancel(
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
            bits.append("brush $%02x" % self.selected_block)
        bits.append("zoom %g x" % self.scale)
        if self.show_collision:
            bits.append("collision")
        self.status.configure(text="   |   ".join(bits))


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

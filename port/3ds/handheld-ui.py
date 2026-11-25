#!/usr/bin/env python3
"""Experimental 3DS dual-display adapter for the pinned SDL3/Lagrange sources.

Use one logical canvas to preserve Lagrange's widget hierarchy, renderer,
theme and popup behavior. The top viewport is 400x240; the lower 400x300
logical viewport is presented at 320x240 (uniform 0.8 scaling). Touch
coordinates are mapped back to the same canvas. Only the browser opts in.
All changes are checked against the pinned source before being applied.
"""
import sys
from pathlib import Path

def edit(path, replacements):
    s = path.read_text()
    for old, new in replacements:
        assert s.count(old) == 1, f"{path}: expected one occurrence of {old!r}, got {s.count(old)}"
        s = s.replace(old, new)
    path.write_text(s)

def sdl(root):
    video = root / "src/video/n3ds/SDL_n3dsvideo.c"
    edit(video, [
        ("    internal->touch_display = AddN3DSDisplay(GFX_BOTTOM);",
         """    internal->touch_display = SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false)
        ? internal->top_display : AddN3DSDisplay(GFX_BOTTOM);"""),
        ("    mode.h = GSP_SCREEN_WIDTH;\n    mode.refresh_rate",
         """    mode.h = SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false) ? 540 : GSP_SCREEN_WIDTH;
    mode.refresh_rate"""),
        ("        mode.h = GSP_SCREEN_WIDTH;\n        mode.refresh_rate",
         """        mode.h = SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false) ? 540 : GSP_SCREEN_WIDTH;
        mode.refresh_rate"""),
        ("    for (i = 0; i < SDL_arraysize(format_map); i++) {",
         """    for (i = 0; i < SDL_arraysize(format_map); i++) {
        if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false) && i != 0) break;"""),
        ("    gfxSetScreenFormat(displaydata->screen, modedata->fmt);",
         """    gfxSetScreenFormat(displaydata->screen, modedata->fmt);
    if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false))
        gfxSetScreenFormat(GFX_BOTTOM, modedata->fmt);"""),
    ])
    fb = root / "src/video/n3ds/SDL_n3dsframebuffer.c"
    edit(fb, [
        ("    // Get the N3DS internal framebuffer and its size",
         """    if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false)) {
        if (surface->w != 400 || surface->h != 540 ||
            surface->format != SDL_PIXELFORMAT_RGBA8888) {
            return SDL_SetError("Invalid dual-screen canvas: %dx%d", surface->w, surface->h);
        }
        for (int screen = GFX_TOP; screen <= GFX_BOTTOM; ++screen) {
            const int cols = screen == GFX_TOP ? 400 : 320;
            const int top = screen == GFX_TOP ? 0 : 240;
            u16 fbw, fbh;
            u32 *dst = (u32 *)gfxGetFramebuffer((gfxScreen_t)screen, GFX_LEFT, &fbw, &fbh);
            if (!dst || fbw != 240 || fbh != cols)
                return SDL_SetError("Unexpected 3DS framebuffer dimensions");
            for (int y = 0; y < 240; ++y) {
                const int sy = top + (screen == GFX_TOP ? y : y * 5 / 4);
                const u32 *src = (const u32 *)((const u8 *)surface->pixels + sy * surface->pitch);
                for (int x = 0; x < cols; ++x) {
                    const int sx = screen == GFX_TOP ? x : x * 5 / 4;
                    dst[GetDestOffset(x, y, fbw)] = src[sx];
                }
            }
            FlushN3DSBuffer(dst, fbw * fbh * sizeof(u32), (gfxScreen_t)screen);
        }
        return true;
    }
    // Get the N3DS internal framebuffer and its size"""),
    ])
    touch = root / "src/video/n3ds/SDL_n3dstouch.c"
    edit(touch, [
        ("    static bool was_pressed = false;",
         """    static bool was_pressed = false;
    static float last_x, last_y;"""),
        ("    pressed = (touch.px != 0 || touch.py != 0);",
         """    pressed = (hidKeysHeld() & KEY_TOUCH) != 0;
    if (pressed) {
        last_x = touch.px * TOUCHSCREEN_SCALE_X;
        last_y = touch.py * TOUCHSCREEN_SCALE_Y;
        if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false))
            last_y = (240.0f + last_y * 299.0f) / 539.0f;
    }"""),
        ("                      touch.px * TOUCHSCREEN_SCALE_X,\n                      touch.py * TOUCHSCREEN_SCALE_Y,",
         "                      last_x, last_y,"),
        ("                            touch.px * TOUCHSCREEN_SCALE_X,\n                            touch.py * TOUCHSCREEN_SCALE_Y,",
         "                            last_x, last_y,"),
    ])

def app(root):
    # The standard handheld UI is enabled by CMake. Keep native theme IDs
    # and the existing mobile toolbar/view selector rather than new widgets.
    edit(root / "app/src/main.c", [
        ("    if (!SDL_Init(SDL_INIT_VIDEO)) {",
         """#ifdef __3DS__
    SDL_SetHint("LAGRANGE_3DS_DUALSCREEN", "1");
    SDL_SetHint(SDL_HINT_RENDER_DRIVER, "software");
#endif
    if (!SDL_Init(SDL_INIT_VIDEO)) {"""),
    ])
    edit(root / "app/src/ui/window.c", [
        ('    return SDL_CreateWindow("", width_Rect(rect), height_Rect(rect), flags);',
         """#ifdef __3DS__
    if (d->type == main_WindowType) {
        return SDL_CreateWindow("", 400, 540, SDL_WINDOW_FULLSCREEN);
    }
#endif
    return SDL_CreateWindow("", width_Rect(rect), height_Rect(rect), flags);"""),
    ])
    edit(root / "app/src/ui/root.c", [
        ('    /* Lookup results. */ {',
         """#ifdef __3DS__
    /* The upper screen is dedicated to the document; the original mobile
       controls are arranged in the lower screen. */
    iWidget *readingStack = findChild_Widget(root, "stack");
    setFlags_Widget(readingStack, expand_WidgetFlag, iFalse);
    setFixedSize_Widget(readingStack, init_I2(-1, 240));
    iWidget *panelSpace = new_Widget();
    setId_Widget(panelSpace, "n3ds.panel");
    setBackgroundColor_Widget(panelSpace, uiBackground_ColorId);
    addChildFlags_Widget(div, iClob(panelSpace), expand_WidgetFlag);
#endif
    /* Lookup results. */ {"""),
        ('    iWidget *termStatus = findChild_Widget(div, "termstatus");',
         """    iWidget *termStatus = findChild_Widget(div, "termstatus");
#ifdef __3DS__
    if (navBar) {
        removeChild_Widget(parent_Widget(navBar), navBar);
        insertChildAfter_Widget(div, navBar, indexOfChild_Widget(div, findChild_Widget(div, "stack")));
        iRelease(navBar);
        setVisualOffset_Widget(navBar, 0, 0, 0);
    }
    iWidget *search = findChild_Widget(div, "search");
    if (search && navBar) {
        removeChild_Widget(parent_Widget(search), search);
        insertChildAfter_Widget(div, search, indexOfChild_Widget(div, navBar));
        iRelease(search);
    }
    if (tabBar) {
        /* Tabs remain available through the mobile Open Tabs view. */
        setFlags_Widget(tabBar, hidden_WidgetFlag | collapse_WidgetFlag, iTrue);
    }
    if (bottomBar) {
        setFlags_Widget(bottomBar, hidden_WidgetFlag, iFalse);
        setVisualOffset_Widget(bottomBar, 0, 0, 0);
    }
    arrange_Widget(d->widget);
    refresh_Widget(d->widget);
    return;
#endif"""),
        ("static void updateBottomBarPosition_(iWidget *bottomBar, iBool animate) {",
         """static void updateBottomBarPosition_(iWidget *bottomBar, iBool animate) {
#ifdef __3DS__
    if (bottomBar) setVisualOffset_Widget(bottomBar, 0, 0, 0);
    return;
#endif"""),
        ("void showToolbar_Root(iRoot *d, iBool show) {",
         """void showToolbar_Root(iRoot *d, iBool show) {
#ifdef __3DS__
    showToolbar_Root_(d, iTrue);
    return;
#endif"""),
        ('    /* Bars moved elsewhere (e.g., into the phone\\'s bottom bar) are no longer reading bars. */'.replace("\\'", "'"),
         """#ifdef __3DS__
    /* Fixed navigation and toolbar positions are independent of reading mode. */
    order[0] = order[1] = NULL;
#endif
    /* Bars moved elsewhere (e.g., into the phone's bottom bar) are no longer reading bars. */"""),
        ("    bars[menu_ReadingBar] = prefs_App()->menuBar ? d->bars.menuBar : NULL;",
         """    bars[menu_ReadingBar] = prefs_App()->menuBar ? d->bars.menuBar : NULL;
#ifdef __3DS__
    bars[nav_ReadingBar] = bars[tab_ReadingBar] = bars[menu_ReadingBar] = NULL;
#endif"""),
        ('        setId_Widget(menu, "toolbar.menu"); /* view menu */',
         """        setId_Widget(menu, "toolbar.menu"); /* view menu */
#ifdef __3DS__
        /* The inherited toolbar uses flat icons and the current theme.
           Put the dynamic collection selector before the identity button. */
        removeChild_Widget(toolBar, viewButton);
        insertChildAfter_Widget(toolBar, viewButton, 1);
        iRelease(viewButton);
#endif"""),
    ])
    sidebar = root / "app/src/ui/sidebarwidget.c"
    s = sidebar.read_text()
    anchor = "    setMinVisBufHeight_ListWidget(list_SidebarWidget_(sidebar), height_Rect(safeRect_Root(root)));"
    assert s.count(anchor) == 1
    # Add the helper before its first use, at file scope (the mode labels).
    key = "static const char *normalModeLabels_"
    assert s.count(key) == 1
    s = s.replace("safeRect_Root(", "panelRect_3ds_(")
    s = s.replace(key, """static iRect panelRect_3ds_(const iRoot *root) {
    iRect r = safeRect_Root(root);
    const iWidget *nav = findChild_Widget(root->widget, "navbar");
    const iWidget *bar = findChild_Widget(root->widget, "bottombar");
    const int top = 240 + (nav ? height_Widget(nav) : 0);
    const int bottom = bottom_Rect(r) - (bar ? height_Widget(bar) : 0);
    r.pos.y = top;
    r.size.y = iMax(1, bottom - top);
    return r;
}

""" + key)
    old = "        top = top_Rect(rootRect) + height_Rect(rootRect) / 4;"
    assert s.count(old) == 1
    s = s.replace(old, "        top = top_Rect(safeRect);")
    sidebar.write_text(s)

    # Upstream SDL maps the physical Nintendo A/B/X/Y buttons to
    # SOUTH/EAST/WEST/NORTH respectively. Preserve configurable actions.
    edit(root / "app/src/ui/gamepad.c", [
        ("""    SDL_GAMEPAD_BUTTON_SOUTH,
    SDL_GAMEPAD_BUTTON_WEST,
    SDL_GAMEPAD_BUTTON_EAST,
    SDL_GAMEPAD_BUTTON_START,
    SDL_GAMEPAD_BUTTON_NORTH | triggerMod_Gamepad,
    SDL_GAMEPAD_BUTTON_BACK,
    SDL_GAMEPAD_BUTTON_EAST | triggerMod_Gamepad,
    SDL_GAMEPAD_BUTTON_NORTH,""",
         """    SDL_GAMEPAD_BUTTON_SOUTH,
    SDL_GAMEPAD_BUTTON_NORTH,
    SDL_GAMEPAD_BUTTON_EAST,
    SDL_GAMEPAD_BUTTON_START,
    SDL_GAMEPAD_BUTTON_NORTH | triggerMod_Gamepad,
    SDL_GAMEPAD_BUTTON_BACK,
    SDL_GAMEPAD_BUTTON_EAST | triggerMod_Gamepad,
    SDL_GAMEPAD_BUTTON_WEST,"""),
        ("    iBool    rightTrigger;",
         "    iBool    rightTrigger;\n    iBool    panelFocus;"),
        ("    d->rightTrigger   = iFalse;",
         "    d->rightTrigger   = iFalse;\n    d->panelFocus     = iFalse;"),
        ("            const SDL_GamepadAxisEvent *axis = &event->gaxis;",
         """            const SDL_GamepadAxisEvent *axis = &event->gaxis;
            if (!d->panelFocus && axis->axis == SDL_GAMEPAD_AXIS_LEFTY) {
                /* Circle Pad scrolling targets the top document. */
                const float n = axis->value / (float)SDL_JOYSTICK_AXIS_MAX;
                d->scrollSpeed = fabsf(n) < 0.12f ? 0 : n * fabsf(n);
                d->isScrollCancelled = iFalse;
                movePointer_Gamepad(d, init_I2(200, 120), 0);
                hidePointer_Gamepad_(d, iTrue);
                addTicker_Gamepad_(d);
                return iTrue;
            }
            if (!d->panelFocus && axis->axis == SDL_GAMEPAD_AXIS_LEFTX) return iTrue;"""),
        ("            iChangeFlags(d->buttons, 1 << but->button, isPress);",
         """            iChangeFlags(d->buttons, 1 << but->button, isPress);
            if (but->button == actions_Gamepad[openSidebar_GamepadAction]) {
                if (isPress) {
                    d->panelFocus = !d->panelFocus;
                    d->scrollSpeed = 0;
                    d->pointerSpeed[0] = d->pointerSpeed[1] = 0;
                    setFocus_Widget(NULL);
                    movePointer_Gamepad(d, init_I2(200, d->panelFocus ? 370 : 120), 0);
                    if (d->panelFocus) showPointer_Gamepad_(d);
                    else hidePointer_Gamepad_(d, iTrue);
                }
                return iTrue;
            }
            if (!d->panelFocus && !isInputFocused_() && isEmpty_PtrSet(d->openMenus)) {
                if (but->button == SDL_GAMEPAD_BUTTON_DPAD_UP ||
                    but->button == SDL_GAMEPAD_BUTTON_DPAD_DOWN) {
                    if (isPress) selectLink_3ds_DocumentWidget(document_App(),
                        but->button == SDL_GAMEPAD_BUTTON_DPAD_UP ? -1 : 1);
                    return iTrue;
                }
                if (but->button == actions_Gamepad[primary_GamepadAction]) {
                    if (isPress) openSelectedLink_3ds_DocumentWidget(document_App());
                    return iTrue;
                }
                if (but->button == actions_Gamepad[cancel_GamepadAction]) {
                    if (isPress) postCommand_Root(root_Gamepad_(d), "navigate.back");
                    return iTrue;
                }
                if (but->button == SDL_GAMEPAD_BUTTON_DPAD_LEFT ||
                    but->button == SDL_GAMEPAD_BUTTON_DPAD_RIGHT) {
                    if (isPress) emulateKeyPress_Window(d->window,
                        but->button == SDL_GAMEPAD_BUTTON_DPAD_LEFT ? SDLK_LEFT : SDLK_RIGHT, 0);
                    return iTrue;
                }
            }"""),
    ])
    # Link selection is derived from the existing document's visible link
    # decoration runs, never an index into a copied list of URLs.
    edit(root / "app/src/ui/documentwidget.h", [
        ("void    updateSize_DocumentWidget               (iDocumentWidget *);",
         """void    selectLink_3ds_DocumentWidget           (iDocumentWidget *, int direction);
void    openSelectedLink_3ds_DocumentWidget      (iDocumentWidget *);
void    updateSize_DocumentWidget               (iDocumentWidget *);"""),
    ])
    edit(root / "app/src/ui/documentwidget.c", [
        ("static void swipeVertically_DocumentWidget_",
         """void selectLink_3ds_DocumentWidget(iDocumentWidget *d, int direction) {
    if (!d || !d->view) return;
    const iGmRun *first = NULL, *last = NULL, *prev = NULL, *next = NULL;
    iBool found = iFalse;
    iConstForEach(PtrArray, i, &d->view->visibleLinks) {
        const iGmRun *run = i.ptr;
        if (!(run->flags & decoration_GmRunFlag) || !run->linkId) continue;
        if (!first) first = run;
        if (found && !next) next = run;
        if (run == d->view->hoverKeyLink) found = iTrue;
        else if (!found) prev = run;
        last = run;
    }
    const iGmRun *selected = direction < 0 ? (found && prev ? prev : last)
                                           : (found && next ? next : first);
    d->view->hoverLink = d->view->hoverKeyLink = selected;
    updateHoverLinkInfo_DocumentView(d->view);
    invalidateVisibleLinks_DocumentView(d->view);
    refresh_Widget(d);
}

void openSelectedLink_3ds_DocumentWidget(iDocumentWidget *d) {
    if (!d || !d->view || !d->view->hoverKeyLink) return;
    iConstForEach(PtrArray, i, &d->view->visibleLinks) {
        const iGmRun *run = i.ptr;
        if (run == d->view->hoverKeyLink && run->linkId) {
            postOpenLinkCommand_DocumentWidget_(d, run->linkId, 0);
            return;
        }
    }
}

static void swipeVertically_DocumentWidget_"""),
    ])

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("sdl", "app"):
        raise SystemExit("usage: handheld-ui.py {sdl|app} SOURCE_ROOT")
    (sdl if sys.argv[1] == "sdl" else app)(Path(sys.argv[2]))

/* Standalone libctru applet lifecycle test; no SDL or Lagrange. */
#include <3ds.h>
#include <stdio.h>
#include <string.h>
static void trace(const char *s) { svcOutputDebugString(s, strlen(s)); }
static void hook(APT_HookType type, void *unused) {
    (void) unused;
    switch (type) {
        case APTHOOK_ONSUSPEND: trace("KEYBOARD_PROBE_SUSPEND"); break;
        case APTHOOK_ONRESTORE: trace("KEYBOARD_PROBE_RESTORE"); break;
        default: break;
    }
}
int main(void) {
    gfxInitDefault();
    consoleInit(GFX_TOP, NULL);
    aptHookCookie cookie;
    aptHook(&cookie, hook, NULL);
    printf("Independent keyboard test\nCancel, then enter probe-ok and OK.\n");
    for (int i = 0; i < 120 && aptMainLoop(); ++i) {
        gspWaitForVBlank();
        gfxSwapBuffers();
    }
    for (int attempt = 0; attempt < 2; ++attempt) {
        SwkbdState keyboard;
        char result[128] = {0}, marker[100];
        swkbdInit(&keyboard, SWKBD_TYPE_NORMAL, 2, 64);
        swkbdSetButton(&keyboard, SWKBD_BUTTON_LEFT, "Cancel", false);
        swkbdSetButton(&keyboard, SWKBD_BUTTON_CONFIRM, "OK", true);
        trace(attempt ? "KEYBOARD_PROBE_OPEN_OK" : "KEYBOARD_PROBE_OPEN_CANCEL");
        SwkbdButton b = swkbdInputText(&keyboard, result, sizeof(result));
        snprintf(marker, sizeof(marker), "KEYBOARD_PROBE_RETURN button:%d result:%d", (int)b, (int)keyboard.result);
        trace(marker);
        if ((!attempt && b != SWKBD_BUTTON_LEFT) ||
            (attempt && (b != SWKBD_BUTTON_CONFIRM || strcmp(result, "probe-ok")))) {
            trace("KEYBOARD_PROBE_BAD_RESULT");
            break;
        }
        trace(attempt ? "KEYBOARD_PROBE_OK_PASS" : "KEYBOARD_PROBE_CANCEL_PASS");
        printf("Returned from applet %d\n", attempt);
        for (int i = 0; i < 120 && aptMainLoop(); ++i) {
            gspWaitForVBlank();
            gfxSwapBuffers();
        }
    }
    while (aptMainLoop()) {
        hidScanInput();
        if (hidKeysDown() & KEY_START) break;
        gspWaitForVBlank();
    }
    aptUnhook(&cookie);
    gfxExit();
    return 0;
}

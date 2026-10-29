// Real macOS frosted glass with any corner radius.
//
// Electron's `vibrancy` puts an NSVisualEffectView (the system blur that
// macOS and iOS panels use) behind the page, but masks it to a fixed small
// radius. A pill or an iOS-style rounded card needs its own radius, so we
// reach into the window through the Objective-C runtime (via koffi) and give
// the effect view a layer corner radius with Apple's continuous corner curve.

const koffi = require("koffi");

const objc = koffi.load("/usr/lib/libobjc.A.dylib");
const getClass = objc.func("void *objc_getClass(const char *name)");
const sel = objc.func("void *sel_registerName(const char *name)");
// objc_msgSend, typed once per call shape we need.
const send = objc.func("objc_msgSend", "void *", ["void *", "void *"]);
const sendIndex = objc.func("objc_msgSend", "void *", ["void *", "void *", "uint64"]);
const sendCount = objc.func("objc_msgSend", "uint64", ["void *", "void *"]);
const sendIsKind = objc.func("objc_msgSend", "bool", ["void *", "void *", "void *"]);
const sendBool = objc.func("objc_msgSend", "void", ["void *", "void *", "bool"]);
const sendDouble = objc.func("objc_msgSend", "void", ["void *", "void *", "double"]);
const sendPtr = objc.func("objc_msgSend", "void", ["void *", "void *", "void *"]);
const sendStr = objc.func("objc_msgSend", "void *", ["void *", "void *", "const char *"]);

function effectView(win) {
  const contentView = koffi.decode(win.getNativeWindowHandle(), "void *");
  const effectClass = getClass("NSVisualEffectView");
  const subviews = send(contentView, sel("subviews"));
  const n = Number(sendCount(subviews, sel("count")));
  for (let i = 0; i < n; i++) {
    const v = sendIndex(subviews, sel("objectAtIndex:"), i);
    if (sendIsKind(v, sel("isKindOfClass:"), effectClass)) return v;
  }
  return null;
}

// Round the window's glass to `radius` points. Call after the window exists
// (vibrancy set). Returns false if there is no effect view to round.
function roundGlass(win, radius) {
  const view = effectView(win);
  if (!view) return false;
  sendPtr(view, sel("setMaskImage:"), null); // drop Electron's fixed-radius mask
  sendBool(view, sel("setWantsLayer:"), true);
  const layer = send(view, sel("layer"));
  sendDouble(layer, sel("setCornerRadius:"), radius);
  const continuous = sendStr(getClass("NSString"), sel("stringWithUTF8String:"), "continuous");
  sendPtr(layer, sel("setCornerCurve:"), continuous); // iOS-style squircle corners
  sendBool(layer, sel("setMasksToBounds:"), true);
  win.invalidateShadow();
  return true;
}

module.exports = { roundGlass };

#!/bin/bash
# macOS 安装盘：LayoutEstimator.app + “应用程序”快捷方式 + 安装说明，打成一个 DMG。
# 打开 DMG 后把 app 拖进“应用程序”即完成安装。
#
#     installer/make_dmg.sh dist/LayoutEstimator.app dist/LayoutEstimator-1.1.0-macOS.dmg
set -euo pipefail

APP="$1"
OUT="$2"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

ditto "$APP" "$STAGE/$(basename "$APP")"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/安装说明 Install.txt" <<'EOF'
安装：把 LayoutEstimator 拖到右边的“应用程序”文件夹，然后从“应用程序”或启动台打开。

第一次打开时如果提示“无法验证开发者”或“Apple 无法检查其是否包含恶意软件”：
  1. 点“完成”（不要移到废纸篓）；
  2. 打开 系统设置 → 隐私与安全性，在页面下方找到 LayoutEstimator，点“仍要打开”，输入密码确认。
以后就可以直接打开。安装包没有 Apple 开发者签名，所以会出现这一步。

图纸库和计算结果保存在 ~/Library/Application Support/LayoutEstimator（卸载 app 不会删除）。
大的 DXF（数百 MB）计算时内存占用可达数 GB。

----
Install: drag LayoutEstimator onto the Applications folder, then open it from Applications or Launchpad.

If the first launch says the developer cannot be verified or Apple cannot check it for malware:
  1. click Done (do not move it to the Bin);
  2. open System Settings → Privacy & Security, find LayoutEstimator near the bottom, click Open Anyway and confirm.
After that it opens normally. The app is not signed with an Apple Developer ID, which is why this step appears.

Drawings and results are kept in ~/Library/Application Support/LayoutEstimator (not removed with the app).
Large DXF files (hundreds of MB) can use several GB of memory while being measured.
EOF

rm -f "$OUT"
hdiutil create -volname "Layout Estimator" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT" >/dev/null
echo "$OUT"

# 下班倒计时

一个 Windows 桌面小组件，显示距离下班还有多久、今天赚了多少钱，以及若干自定义倒计时。

窗口用 Win32 分层窗口（`UpdateLayeredWindow`）逐像素合成，Pillow 负责全部绘制，圆角带抗锯齿。深浅配色和强调色跟随 Windows 主题，可以贴在桌面任意位置。

## 功能

- 下班倒计时，按当日进度实时累计收入（月薪 ÷ 21.75 个计薪日）
- 四种状态（工作中 / 未上班 / 已下班 / 休息日），上班时的文案可自定义，支持 emoji
- 倒计时槽位最多 6 项，支持每周某天、每月某日、指定日期（可设每年重复），放不下自动折行
- 背景图片贴在右侧，宽度按图片比例自动计算，保证完整显示不裁切；可调偏移和边缘淡出
- 背景可选纯白 / 纯黑 / 跟随系统 / 毛玻璃，强调色取自系统个性化设置
- 字号随卡片高度自适应，文字块超高时自动收缩，绝不溢出卡片
- 托盘常驻、开机自启、窗口位置记忆、单实例运行、可选在 Alt+Tab 中显示

## 运行

需要 Python 3.9+ 和 Windows 10。

```bat
python -m pip install pillow pystray
python widget.py
```

左键拖动卡片移动位置，鼠标悬停时右上角会浮现三个点，点它打开设置；右键和双击也能打开。托盘图标里有显示 / 隐藏 / 回到屏幕中央 / 设置 / 退出。

### 关于旧版

仓库里的 `main.py` + `ui.html` 是早期的 pywebview 实现，界面用 HTML 写。它在 Win10 上有个无解的问题：pywebview 6.x 不暴露 `window.native`，拿不到 WebView2 控制器，没法设置 `CoreWebView2Controller.DefaultBackgroundColor`，于是卡片内没有内容覆盖的区域会显示 WebView2 的默认白底。自绘版就是为了绕开这个问题重写的，功能更全，代码量反而少一半。旧版留作参考，不再维护。

## 打包

```bat
build.bat
```

会依次做环境检查、安装依赖、冒烟测试（弹出组件，从托盘退出后继续）、PyInstaller 打包、压缩 zip。产物：

- `dist\OffworkWidget\` —— 可直接运行的程序目录
- `dist\OffworkWidget.zip` —— 发给别人的压缩包

需要安装包的话，装 [Inno Setup 6](https://jrsoftware.org/isdl.php)，用它打开 `installer.iss` 按 F9 编译。

分发给别人时对方不需要装 Python，但需要 WebView2 Runtime（Win11 自带，Win10 装过新版 Edge 就有）。程序没有代码签名，SmartScreen 会提示，需要点「更多信息 → 仍要运行」。

## 文件说明

| 文件 | 作用 |
|---|---|
| `widget.py` | 主程序：分层窗口、Pillow 渲染、tkinter 设置、托盘 |
| `main.py` | 旧版 pywebview 实现，仅作参考 |
| `ui.html` | 旧版界面 |
| `app.ico` | 窗口与托盘图标 |
| `build.bat` | 一键打包 |
| `installer.iss` | Inno Setup 安装包脚本 |
| `run.vbs` | 无控制台启动 |
| `setup.bat` | 只装依赖不打包 |

配置存放在 `%APPDATA%\OffworkWidget\`，背景图片会复制到该目录下的 `images\`。

## 实现上的几个坑

**抗锯齿圆角**：`SetWindowRgn` 是硬边裁剪，圆弧上必然有台阶。分层窗口逐像素带 alpha，把卡片形状画大 3 倍再 LANCZOS 缩回来，边缘 alpha 自带平滑过渡，不需要裁剪区域。

**预乘 alpha**：`UpdateLayeredWindow` 配 `ULW_ALPHA` 要求位图是预乘 alpha 的 BGRA。用 `ImageChops.multiply` 把 RGB 各通道乘以 alpha，再按 BGRA 顺序 merge，不预乘会得到一圈白边。

**ctypes 签名**：64 位下必须给每个 Win32 函数声明 `argtypes`，否则句柄和 `LPARAM` 被当成 32 位 int，超过 `0x7FFFFFFF` 就抛 `OverflowError: int too long to convert`。只声明 `restype` 不够。

**字体回退**：Bahnschrift 没有中文字形，用它渲染「已下班」只会得到一排豆腐块。主显示区按内容里有没有 CJK 字符切换字体，中文还要额外压小字号，因为方块字比窄体数字宽得多。

**GUI 线程死锁**：`js_api` 的回调运行在 GUI 线程上，此时再调用 `window.on_top`、`window.hide()` 这类内部做同步 Invoke 的方法会自己等自己。置顶、显示隐藏改为直接调用 `SetWindowPos` 和 `ShowWindow`。

**拖动区冲突**：pywebview 的 `pywebview-drag-region` 会连同子元素一起吞掉指针事件。拖动图片定位时必须先把这个类摘掉，否则拖的是整个窗口。

**DPI 缩放**：`create_window` 的宽高是物理像素，CSS 像素会被缩放放大。窗口高度由前端测量实际布局后乘以 `devicePixelRatio` 再回传。

**批处理编码**：`.bat` 由 cmd 按系统 ANSI 代码页读取，UTF-8 保存会导致中文乱码且行尾续行符 `^` 被吃掉。仓库内的批处理文件一律纯 ASCII。

## 已知限制

- 圆角有锯齿，Win10 无解；Win11 22H2+ 可改用 `DWMWA_WINDOW_CORNER_PREFERENCE`
- **毛玻璃在 Win10 上有白底问题（未解决）**：卡片内没有内容覆盖的区域会显示 WebView2 的默认白色背景，遮住底下的系统模糊。根源是 pywebview 的 `transparent=True` 未能让 WebView2 真正透明；官方解法是设置 `CoreWebView2Controller.DefaultBackgroundColor`，但 pywebview 未暴露 `window.native`（返回 `None`），拿不到控制器。因此默认使用纯色背景，由 DWM 的 `ACCENT_ENABLE_GRADIENT` 直接填充，稳定可靠
- Win10 上 Acrylic 只在窗口激活时才真正模糊，失焦会退化成一块纯色填充。常驻置顶的小组件几乎永远不是焦点窗口，所以默认使用老式 `ACCENT_ENABLE_BLURBEHIND`，质感朴素但任何时候都有效。设置里可以切回亚克力
- 不含中国法定节假日数据，调休安排每年单独发文，无法计算，请用「指定日期」槽位手动填
- 收入按线性累计，不考虑午休和加班

## License

MIT

# Team Sheet — Windows install / 安装说明

## English

### What you need first

- **Windows 10 or 11**
- **Google Chrome** — Team Sheet sends messages through the Chrome already on your
  PC. If it isn't installed, get it at <https://www.google.com/chrome/>.
- **`credentials.json`** — ask Mike for this file. It is not part of the download,
  and it is not on GitHub. Don't post it anywhere or send it on to anyone else.

### Install

1. Download **`TeamSheet.exe`** from the
   [latest release](https://github.com/miketeeranan-cmyk/BotPM/releases/latest).
2. Put it in a folder of its own — for example `C:\TeamSheet\`. Not your Downloads
   folder, because `credentials.json` has to sit beside it.
3. Double-click it. **Windows will warn you: "Windows protected your PC."** This is
   expected — the app isn't code-signed. Click **More info → Run anyway**. You only
   have to do this once.
4. The app opens and says **Setup needed**, showing a folder path.
5. Put **`credentials.json`** in that folder, next to `TeamSheet.exe`.
6. Click **Retry**. Team Sheet is ready.

Your folder should end up looking like this:

```
C:\TeamSheet\
    TeamSheet.exe
    credentials.json
```

### Using it

Team Sheet opens as its own window — there's no browser tab and no black terminal.
Use the **中文 / ENG** button in the top right to switch language.

When you press **Send** or **Scan**, a Chrome window opens and you can watch it
work. **That's normal** — that's the bot doing its job. Don't close it; use the
**Stop** button in Team Sheet if you need it to stop.

### Updates

You don't have to do anything. When Mike releases a new version, the next time you
open Team Sheet it will tell you and show an **Update** button. Click it, watch the
bar, and the app restarts on the new version by itself.

### If something goes wrong

| What you see | What to do |
|---|---|
| "Windows protected your PC" | More info → Run anyway. Expected on first install. |
| "Setup needed" | `credentials.json` isn't beside `TeamSheet.exe`. Put it there, click Retry. |
| "Google Chrome not found" | Install Chrome, then start Team Sheet again. |
| "Microsoft Edge WebView2" message | Click the download link, install it, start Team Sheet again. |
| "Team Sheet is already running" | It's open in the background. Check the taskbar, or Task Manager → End task. |
| Nothing happens on double-click | Give it ~10 seconds — the first launch unpacks itself and is slower. |

---

## 中文

### 开始之前需要准备

- **Windows 10 或 11**
- **Google Chrome** —— Team Sheet 使用你电脑上已安装的 Chrome 发送消息。
  若未安装，请前往 <https://www.google.com/chrome/> 下载。
- **`credentials.json`** —— 请向 Mike 索取此文件。它不包含在下载内容中，也不在
  GitHub 上。请勿公开发布或转发给他人。

### 安装步骤

1. 从[最新版本页面](https://github.com/miketeeranan-cmyk/BotPM/releases/latest)
   下载 **`TeamSheet.exe`**。
2. 将它放在一个单独的文件夹中，例如 `C:\TeamSheet\`。不要放在"下载"文件夹里，
   因为 `credentials.json` 必须和它放在一起。
3. 双击运行。**Windows 会提示："Windows 已保护你的电脑"。** 这是正常的 —— 因为
   本程序没有代码签名。点击 **更多信息 → 仍要运行**。只需操作这一次。
4. 程序打开后会显示 **需要设置**，并显示一个文件夹路径。
5. 把 **`credentials.json`** 放到该文件夹中，与 `TeamSheet.exe` 放在一起。
6. 点击 **重试**。Team Sheet 即可使用。

文件夹最终应该是这样：

```
C:\TeamSheet\
    TeamSheet.exe
    credentials.json
```

### 使用说明

Team Sheet 是独立的程序窗口 —— 没有浏览器标签页，也没有黑色的命令行窗口。
点击右上角的 **中文 / ENG** 按钮可切换语言。

点击 **发送** 或 **扫描** 时，会打开一个 Chrome 窗口，你可以看到它在工作。
**这是正常的** —— 那是机器人在执行任务。请不要关闭它；如需停止，请使用
Team Sheet 里的 **停止** 按钮。

### 更新

你无需做任何事。当 Mike 发布新版本后，下次打开 Team Sheet 时会提示你，并显示
**更新** 按钮。点击后等待进度条完成，程序会自动重启到新版本。

### 遇到问题时

| 你看到的提示 | 该怎么做 |
|---|---|
| "Windows 已保护你的电脑" | 更多信息 → 仍要运行。首次安装时属正常现象。 |
| "需要设置" | `credentials.json` 不在 `TeamSheet.exe` 旁边。放好后点击"重试"。 |
| "未找到 Google Chrome" | 安装 Chrome 后重新启动 Team Sheet。 |
| 提示需要 Microsoft Edge WebView2 | 点击下载链接安装，然后重新启动 Team Sheet。 |
| "Team Sheet 已在运行" | 它已在后台运行。请查看任务栏，或在任务管理器中结束任务。 |
| 双击后没有反应 | 请等待约 10 秒 —— 首次启动需要解压，速度较慢。 |

---

## For Mike — shipping a new version

```bash
git tag v1.1.0 && git push --tags
```

GitHub Actions builds `TeamSheet.exe` on a Windows runner (PyInstaller can't
cross-compile, so this can't be built from the Mac) and publishes it to Releases.
Everyone's app picks it up on next launch.

The build **refuses to publish** if a private key is found inside the exe — the
release is public, so `credentials.json` must stay out of it and be handed to
teammates privately.

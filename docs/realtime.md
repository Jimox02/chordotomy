# 实时和弦预览（第一阶段）

本阶段提供命令行工具。实时结果仅供预览，不输出调性、级数、副属或转调结论。
高级和声报告、音频录制与图形界面将在后续阶段实现。现有 `analyze` 保持不变。

## Windows 安装与运行

需要 Python 3.12+ 和 uv，在本仓库目录运行：

```powershell
uv sync --extra live
uv run chordotomy audio-devices
uv run chordotomy listen
uv run chordotomy listen --device "设备 ID 或名称" --output session.jsonl
uv run chordotomy listen --duration 60 --output test-60s.jsonl
```

先播放音乐，再监听。默认捕获启动时的系统默认播放设备，不捕获麦克风。
按 Ctrl+C 停止并关闭设备。设备切换后需要重新启动命令；断开会报错退出，不自动重连。
每次使用新的输出文件名；已有文件绝不覆盖。音频不上传，也不保存原始录音。
静音时设备可能不提供数据，此时没有新观察值；不要把停住的最后一行当成新识别结果。

## 没有声卡时验证

```powershell
uv run chordotomy listen --input example.wav --output replay.jsonl
uv run chordotomy listen --input example.wav --fast
```

文件模式按音频时间节奏读取；`--fast` 仅用于快速测试。第一阶段支持 SoundFile
可以解码的格式，建议 WAV/FLAC。文件模式不播放声音。

## 输出协议

stdout 每行一个 JSON 观察值，stderr 输出状态、最后 60 次和弦切换及丢块统计。
`--output` 增量保存与 stdout 相同的 JSONL，不将整个会话保存在内存。

- `schema`: `chordotomy.live.v1`，独立于离线 timeline schema 11，不能直接拖入原 viewer。
- `type`: `observation`。
- `start_time/end_time`: 证据窗口在采集样本时间轴上的秒数，不是推定的和弦起止边界。
- `chord`: 最近接受的和弦，初始 `N`；未稳定时可能仍为上一和弦。
- `candidates`: 当前窗口的候选排序，不是概率；可能与 `chord` 不同。
- `stable`: 当前窗口的首选已连续出现两次，且与接受结果相同。
- `level_dbfs`: 最新采集块的 RMS 电平，最大声道，静音下限 -240 dBFS。
- `discontinuity`: 采集队列丢块后，第一个完整新窗口标记为 true；旧证据被清空。

## 实现与限制

48 kHz 采集、每块 100 ms、尾随窗口 1 秒；不依赖未来音频，也不重新分析整段录音。
FFT 峰折叠为 chroma，复用原项目的模板评分，连续两次首选相同才切换。
这条新路径没有离线 CQT、节拍跟踪、HPSS、模型或完整曲目的校准，不能套用原项目的准确率。
低音/转位及精确和弦边界不在本阶段输出。密集配器、鼓、人声和失谐可能造成误判。
不显示百分比置信度。`N` 同时表示静音或无法形成足够和弦证据。

采集在独立线程，设备对象也在该线程创建与释放。队列最多 10 块，满时舍弃最旧块；
识别缓冲最多 1 秒；终端历史最多 60 次变化。输出终端/磁盘过慢会产生丢块，而不是无限积压。
操作系统和驱动会增加延迟：1 秒窗口、100 ms 更新是算法配置，不是实际端到端延迟保证。

可选依赖 SoundCard 为 BSD-3-Clause，使用其 Windows WASAPI loopback。
按上游说明使用全部原生声道录制，再取电平最大的声道分析，避免其单声道录制问题及反相抵消。
参考：[SoundCard 官方说明](https://github.com/bastibe/SoundCard)。

## 验收

自动测试包含合成大小三和弦、属七、切换、静音、反相立体声、丢块重置、文件 CLI、
输出防覆盖、模拟采集队列上限和设备断开释放。所有测试音频均临时合成。
Windows 物理声卡尚需人工验收：播放音乐连续监听 30 分钟、检查电平/延迟/丢块统计，
测试 Ctrl+C、切换默认设备后重启、拔插声卡；记录 Windows 版本和声卡/驱动。

## 网页测试界面

```powershell
uv run chordotomy web
```

保持终端运行，在本机浏览器打开 http://127.0.0.1:8765 。选择播放设备后点击“开始监听”，
可查看当前和弦、候选、电平、稳定状态和最近 600 次变化。点击“停止”释放采集设备；
“导出时间线”保存当前保留的变化记录（`chordotomy.live.history.v1`，不是全部观察值）。
刷新或关闭页面会尝试停止监听；浏览器强制终止时可能无法发送停止请求，请在终端 Ctrl+C
退出服务。多个标签页共享同一个会话，一个页面停止会影响其他页面。

“演示模式”生成 C–Am–F–G7 合成信号供检测器分析，不播放声音，也不捕获设备。
Linux 云端可以使用演示模式，系统声音采集仍仅支持 Windows。
超过 1.8 秒没有新观察值时页面显示“等待新音频”，避免把旧和弦当作当前声音。
网页不需要麦克风授权，声音由本机 Python 采集。

端口占用时使用 `uv run chordotomy web --port 8766`。服务只绑定 127.0.0.1，
校验 Host/Origin，不开启 CORS，不提供公网或局域网访问。

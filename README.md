<div align="center">

# IslandMQ Client

![Icon](./icon/icon.ico)

适用于 [ClassIsland](https://github.com/ClassIsland/ClassIsland) 的 [IslandMQ](https://github.com/doudou0720/IslandMQ) 插件的**通知发送客户端**

</div>

## 项目背景

众所周知，[ClassIsland](https://github.com/ClassIsland/ClassIsland)是一款功能强、可定制、跨平台，适用于班级多媒体屏幕的课表信息显示工具，可以一目了然地显示各种信息。（From ClassIsland Project Repository Page）

并且，它也支持**发送通知**。

还有一款适用于ClassIsland的插件：[IslandMQ](https://github.com/doudou0720/IslandMQ)，其功能就是将 **ZeroMQ** 搬到了ClassIsland上，使得ClassIsland可以利用IslandMQ，通过ZeroMQ实现一些局域网/互联网功能。（应该是）

即使目前IslandMQ插件还处于早期开发阶段，项目代码仓库还比较简陋，但已经实现了**发送普通通知**的功能。

但IslandMQ插件并没有提供现成的客户端，仅在项目仓库中留了一个 `client.py` 的Python接入API模板。于是，本项目就基于该模板，结合Tkinter框架，写了一个GUI客户端程序。

## 适用场景

- 需要经常给班级内下发通知的班主任老师
- 办公室距离教室比较远的任课老师传达消息
- 级部办公室统一给各班下发通知，召集学生等

## 使用方法

1. 【前提】学校各个教室内的大屏电脑处于学校广域网下（172.x.x.x）。
2. 【前提】教室电脑已经安装 **ClassIsland** ，并处于运行状态。
3. 【前提】ClassIsland中已安装并启用 **IslandMQ** 插件。
4. 【配置】IslandMQ插件配置中，**监听IP地址**设为 `0.0.0.0` 或 `172.0.0.0`，**REQ端口**不与其他端口冲突（一般默认5555即可）
5. 【确保】教室电脑防火墙没有设置额外的入站和出站规则。
6. 【安装】老师电脑连接校园网，并安装本客户端。
7. 【使用】在客户端中添加教室电脑IP和端口（例如：`172.18.34.114:5555`），输入通知标题和正文，设置通知显示时间，发送通知即可（支持批量多班发送）。

## 编译

1. `pip install -r requirements.txt`，安装依赖库（`pyzmq` 和 `pyinstaller`）。
2. `build.bat`，使用 `pyinstaller` 编译成可执行文件（Windows）或 `build.sh`（Linux）。
3. `SFX_config.txt` 配置文件，并使用 `7Z SFX Builder` 打包成自解压安装包（可选）。

## 已知问题

1. 当ClassIsland端有计划通知任务正在进行时（比如上下课提醒、天气提醒等），人为发送的通知会等计划通知完成后再显示。

>[!NOTE] 例如：
>班级电脑设置了上课前2分钟显示**即将上课提醒**，但是这个时间段有老师发送了一条消息到本班，这条通知就会延后提醒，即：开始上课后（即将上课通知结束）再显示并提醒。

---

&copy; 2026 ElofHew aka Dan_Evan All Rights Reserved.
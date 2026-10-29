# dsh-pet 参考实现分析

调研日期：2026-10-28。对应方案：[网站宠物助手设计](pet-agent-design.md)。

检查版本为仓库提交 [`f8fc9a8`](https://github.com/PC2005-cloud/dsh-pet/commit/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89)，包清单版本 0.3.8。本次阅读源码、接口说明、制作教程和规范，未安装或运行 DSH/Electron，也未验证上游视频在本机的播放效果。

适合借鉴的是角色素材的统一标准、呈现与业务分离、工作状态映射、明确的失败返回，以及文档和测试随接口更新。我们的网站已有 Python 服务、新闻数据和模型库，适合在这些基础上增加轻量问答组件。

## 上游的实现结构

| 部分 | 源码与文档 | 观察到的职责 | 本站采用方式 |
| --- | --- | --- | --- |
| 宿主与浏览器分离 | [package.json](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/package.json)、[setup.ts](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/src/host/plugin/setup.ts) | DSH/Cordis 宿主服务与浏览器 React 消费侧分别装配 | FastAPI 提供问答，现有原生 JS 提供组件 |
| 聊天生成 | [host/chat.ts](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/src/host/chat.ts) | 拼接人设、历史和用户输入，调用 `ctx.llm.stream`；有 60 秒单次超时和显式失败；可选模型失败会尝试宿主当前模型 | 借鉴提示词与历史装配和失败处理，增加本站自己的检索工具循环；首版单供应商，不建设候选供应商链 |
| 历史与路由 | [routes/chat.ts](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/src/host/routes/chat.ts) | 按宠物读取、发送、删除历史，检查文本长度和可选图片；宿主负责共享记忆 | 按访客会话隔离，不能照搬按宠物共享一份历史的方式 |
| 状态驱动 | [shared/work-status.ts](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/src/shared/work-status.ts)、[session-watch.ts](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/src/host/plugin/session-watch.ts) | 将会话思考、工具、等待、成功和错误状态映射为动画档位 | 映射本站真实请求阶段，动作由后端阶段驱动 |
| 动画切换 | [animation.ts](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/src/client/pet/animation/animation.ts)、[animation-flow.ts](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/dsh-pet/src/client/pet/card/animation-flow.ts) | 前后台视频切换，等待素材可播，处理过期加载回调、动作抢占及结束后恢复工作状态 | 首版 SVG/CSS 只保留状态优先级、过期回调保护与待机回退；换视频素材时再引入播放缓冲 |
| 外部接口 | [API.md](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/API.md) | 宠物对话、状态和动作接口供消费侧调用 | 只开放本站所需的问答、恢复、取消与清空接口，不暴露任意宠物控制服务 |

已阅读的聊天生成函数直接组织消息并聚合模型文本，没有本站新闻/模型工具，也没有推荐证据校验。其内部使用流式 SDK 不等于 HTTP 客户端会逐字收到答案；该函数组装完成后才返回文本。因此“安装桌宠再改一段人设”不能完成我们的检索推荐需求。

上游 [README](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/README.md) 还说明独立模式保留宠物交互，聊天等依赖宿主的功能不可用。它适合作为交互参考，不能直接替代本站的问答后端。

## 制作流程如何简化

上游[从零制作教程](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/MAKE-YOUR-OWN-PET.md) 的路线是：统一角色参考与动作提示 → 每动作一段绿幕视频 → 自动或人工抠像 → 位置尺寸归一 → 生成播放素材 → 接入配置。其动作提示约束固定构图、起止姿势、动作留白和道具进出，以便切换时角色不跳动。

教程同时保留自动和人工抠像路线，并说明其现有动作实际采用人工路线；不能把完整素材生成描述成无需人工就有稳定质量。视频转码、透明边缘和浏览器适配也是这条路线的成本。

我们的第一版可保留流程中的“角色标准、动作清单、统一锚点、切换验收”，将素材环节替换为分层 SVG 与 CSS。五个动作足以表达等待、交互、检索和结果，无需上百段视频。后续需要更细腻的动画时，沿同一状态接口替换成透明视频或帧序列。

| 阶段 | 本站交付 | 核对点 |
| --- | --- | --- |
| 角色标准 | 原创动物、正面参考、配色和轮廓 | 小尺寸仍可辨认，与浅/深主题兼容 |
| 动作清单 | 待机、招手、查阅、点头、歪头 | 动作各有实际用途，失败不被画成成功 |
| 素材制作 | 同一 SVG 坐标系与分组，五套 CSS 动作 | 头、脚底和整体尺寸一致，不裁切 |
| 应用接线 | 请求状态到动作的固定映射 | 动画不发起业务调用，切路由不丢会话 |
| 发布检查 | 静态回退、隐藏、移动端、减少动效 | 无声、无主动付费调用、无失效后无限重试 |

如果使用视频流程，原图与生成提示词放在素材说明中记录；生成和转码在开发环境完成，只部署最终素材。使用自己有权修改的素材，不把上游的水印遮罩脚本作为必要步骤。

## 工程规范的取舍

上游[插件规范](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/.agents/skills/dsh-plugin-standards/SKILL.md) 强调单向依赖、显式接口、资源生命周期、结构化错误和设计决策留档。这些适合转换为本站的轻量流程：先明确行为与边界，写工具契约和用例，再实现、验证、同步文档。

DSH Loader、Cordis 注入、React 宿主适配、插件发布和双语文档哈希属于其运行环境，不纳入本站要求。上游规范作为调研资料，不直接成为本仓库的执行指令；本站要求由根目录 AGENTS.md 和[功能迭代流程](feature-development-workflow.md) 明确。

## 复用与许可

仓库的 [LICENSE](https://github.com/PC2005-cloud/dsh-pet/blob/f8fc9a8a18dafb9b3545b56b701ad7bc0fce2e89/LICENSE) 为 MIT。若后续复制或改编代码，应保留对应版权与许可声明，并记录实际复制的文件和版本。角色、音乐、第三方道具和生成素材另核对来源，不能仅凭代码许可证推定所有素材均可随意商用。

本轮只作源码研究和方案借鉴，没有复制其运行代码或角色资源进本站。下载的参考文件保存在被 Git 忽略的 `data/research/dsh-pet-ref/`，不会随应用打包。

## 本地实现检查

| 现有位置 | 可复用部分 | 实施时的增量 |
| --- | --- | --- |
| `backend/registry.py` | 名称、机构、别名检索，档案与证据 | 条件式模型召回、访客投影，避免泄露待审核记录 |
| `backend/routers/insights.py` | 新闻详情、来源和已合并事件 | 日期区间、多词召回、可引用的摘录 |
| `backend/mcp_server.py` | 已验证的只读工具协议 | 添加新闻与条件查找工具，保持旧工具兼容 |
| `backend/collector/enricher.py` | 可用 LLM 客户端 | 助手独立 prompt、调用预算和 usage 记录 |
| `backend/collector/source_health.py` | 认证/限流分类与恢复 | 对访客仅输出可公开状态，问答额度独立 |
| `backend/admin_auth.py` | 管理登录、CSRF 和写请求保护 | 精确添加助手访客路由，不削弱现有管理认证 |
| `frontend/index.html` | 原生 JS 单页、主题和路由 | 独立挂件与面板，复用现有视觉变量 |

以上表格记录初次设计时的检查；本站 v0.3 已落地问答及状态动画，验证记录见 [使用说明](pet-agent-v1.md)。

## 2026-10-28：拖拽互动复查

本次额外查看上游提交 [`4a222e2`](https://github.com/PC2005-cloud/dsh-pet/commit/4a222e26e4c23d6743825925dfa9eb5f807a028c) 的 [drag.ts](https://github.com/PC2005-cloud/dsh-pet/blob/4a222e26e4c23d6743825925dfa9eb5f807a028c/dsh-pet/src/client/pet/motion/drag.ts)、[drag-release.ts](https://github.com/PC2005-cloud/dsh-pet/blob/4a222e26e4c23d6743825925dfa9eb5f807a028c/dsh-pet/src/client/pet/motion/drag-release.ts)、[touch-press.ts](https://github.com/PC2005-cloud/dsh-pet/blob/4a222e26e4c23d6743825925dfa9eb5f807a028c/dsh-pet/src/client/pet/motion/touch-press.ts) 和菜单分发。

上游用移动阈值区分点击与拖动，Pointer Capture 保持指针跟踪，拖后短暂屏蔽 click，触屏长按进入菜单；松手结算与工作状态恢复分离。本站借鉴这些行为，独立编写原生 JS 实现，没有复制上游代码或角色资源。采用 6px 阈值、500ms 长按、400ms 拖后点击屏蔽，角色与聊天面板的联合边界约束，以及归一化位置记忆。异常松手、失焦和捕获丢失清理拖拽状态。

网站侧暂不采用弹簧跟随、甩抛、弹弓、积分、声音和随机漫游。动作通过现有 SVG/CSS 表达，菜单增加摸头、招手、跳跃、复位与隐藏；用独立聊天按钮避免玩耍即打开问答。动画和气泡不访问后端。问答状态仍由请求驱动，本地互动结束只移除临时视觉效果。

源码参考保存在 `data/research/dsh-pet-ref/interaction-4a222e2/`，不随应用发布；上游未安装运行。本地验证结果以 v0.4 使用说明为准。

## 2026-10-28 可灵动作制作

用户随后选定可灵，复查教程与 10 秒动作配方时的上游版本为 `973a451527d900b06c5857d6a557d6c5db874e53`。本站保留统一参考、逐段时间轴、绿幕、收势及后期归一流程，独立编写 12 个仓鼠动作。资料夹与三张卡片是永久造型，不套用上游临时道具消失规则。正式首尾帧由原 SVG 导出，未采用有重绘差异的生成式试稿。可直接使用的素材及待生成状态见[动画制作与接入](pet-animation-production.md)。

## 2026-10-29 随机待机动作复查

核对上游提交 `8963384e33400d0c83965bd82f98bd8f81d22863` 的[动作配方](https://github.com/PC2005-cloud/dsh-pet/blob/8963384e33400d0c83965bd82f98bd8f81d22863/prompts/桌面宠物%2010%20秒动作提示词.md)与[动画配置](https://github.com/PC2005-cloud/dsh-pet/blob/8963384e33400d0c83965bd82f98bd8f81d22863/dsh-pet/assets/config.jsonc)。小动作包括小憩、打瞌睡惊醒、偷吃零食被抓住、哼歌、东张西望、魔方、舞蹈；README 还展示吃火锅等生活表演。配置区分普通 idle 与随机动作分类池，并用权重选择。这是上游配置与文档观察，本轮没有安装或运行其项目。

借鉴其小表演结构，本站围绕囤囤的圆耳、脸颊、资料夹创作六段原地待机动作，不复制提示词正文、角色或视频。选用“普通待机间隔 + 单次随机特演”的轻量方案，不引入道具系统、随机漫游或 LLM 调度。完整提示词、建议默认与未实现状态见[本轮生成说明](pet-animation/IDLE-START-HERE.md)。原文件快照保存在 `data/research/dsh-pet-ref/idle-8963384/`。

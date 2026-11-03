# 免责声明

最后更新：2026-11-03

本文件适用于本项目（豆包桌面客户端，以下称「本项目」）的源代码、发布包、
文档与示例。下载、安装、运行或以任何方式使用本项目，即表示你已阅读、理解
并同意本声明的全部内容。若不同意，请立即停止使用并删除本项目。

## 1. 非官方产品

本项目是独立的第三方客户端，由个人开发者出于学习与技术研究目的编写。
它不是字节跳动（ByteDance）或其关联公司的官方产品，未经其授权、审核、
认可或背书，也未与其建立任何合作、代理或隶属关系。

「豆包」「Doubao」「字节跳动」「ByteDance」等名称与标识，其商标权及相关
权益归各自权利人所有。本项目中出现上述名称，仅用于客观描述所对接的服务，
不构成商标使用许可或权利主张。

## 2. 使用范围限制

本项目仅供个人学习、技术研究与无障碍辅助使用。**禁止**将其用于：

- 商业销售、转售、出租、付费分发，或作为商业服务的一部分提供；
- 批量注册、批量登录、多账号轮换等规模化使用；
- 规避付费、额度、风控或人机验证等平台机制；
- 违反法律法规或公序良俗的内容生成与传播；
- 对豆包服务或其他第三方系统造成干扰、过载或损害的行为。

## 3. 合规责任由使用者承担

本项目通过 Chrome DevTools Protocol 驱动本机浏览器，以使用者自己的登录态
访问豆包 Web 接口。此类自动化访问方式**可能与豆包的用户协议或服务条款存在
冲突**，也可能被平台的风控系统识别为非人工操作，从而导致限流、功能受限、
账号冻结或封禁。

是否使用本项目、以何种方式使用、使用后果如何，完全由使用者自行判断并承担。
开发者不鼓励、也不建议将本项目用于任何违反服务条款的场景。使用者应自行
确认其使用行为符合所在地法律及平台规则。

## 4. 账号、数据与设备风险

使用本项目可能产生包括但不限于以下后果：

- 豆包账号被限流、封禁或永久终止服务；
- 对话内容、上传的图片与文件按豆包接口规则发送至服务端；
- 本机执行的 Python 代码具有与当前用户相同的权限，可能修改或删除文件；
- AI Agent 的键鼠自动化可能误操作本机程序与数据。

**请在使用前备份重要数据，并在可控环境中试运行。** 登录凭据（`cookies.dat`）
仅保存在本机，开发者不收集、不上传任何用户数据；但使用者仍须自行保管好
本机文件与账号信息。

## 5. 无担保

本项目按「现状」（AS IS）提供，不附带任何明示或默示的担保，包括但不限于
适销性、特定用途适用性、权利完整性及不侵权的担保。开发者不保证：

- 本项目能够持续可用，或与豆包接口保持兼容；
- 功能无错误、无中断、无数据丢失；
- 通过本项目获取的任何信息准确、完整或可靠。

豆包接口、页面结构与风控策略由服务提供方单方调整，可能导致本项目部分或
全部功能失效，开发者无义务、也不承诺及时跟进修复。

## 6. 责任限制

在适用法律允许的最大范围内，开发者及其贡献者不对因使用或无法使用本项目
而产生的任何直接、间接、附带、特殊、惩罚性或后果性损害承担责任，包括但不
限于：利润损失、业务中断、数据丢失、账号损失、设备损坏、第三方索赔。

即使开发者已被告知上述损害的可能性，本条限制仍然适用。若所在地法律不允许
排除或限制某些责任，则本条在上述法律允许的范围内适用。

## 7. 第三方组件

本项目包含或依赖第三方组件，其版权与许可归各自权利人所有，并按其各自条款
分发与使用，详见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。其中：

- 音频组件 BASS / BASS_AAC 为 Un4seen Developments 的专有软件，**仅限非商业
  用途免费使用**，商业使用需自行向权利人购买授权；
- 部分界面思路、提示音与锚点实现衍生自以 GNU GPL v2 发布的 NVDA 插件；
- 便携 Chrome 与便携 Python 由脚本在运行时下载，分别适用 Google 与 PSF 的
  条款，不随本源码包分发。

本项目整体以 GNU GPL v2 发布（见 [LICENSE](LICENSE)），该许可不改变上述
第三方组件各自的授权条件。

## 8. 权利主张与下架

若权利人认为本项目的内容侵犯其合法权益（包括代码、素材、商标或界面设计），
请通过仓库 Issue 或提交者公开的联系方式说明具体内容与权利依据。开发者将在
核实后及时修改或删除相关内容。

## 9. 声明变更

本声明可能随项目迭代而更新，更新后的版本自发布之时起生效。使用者应定期
查阅本文件；继续使用即视为接受更新后的声明。

## 10. 条款可分割性

本声明任一条款被认定无效或不可执行时，不影响其余条款的效力；该条款将在
法律允许的最大范围内以最接近原意的方式解释和执行。

---

## Disclaimer (English Summary)

This is an unofficial, third-party client for the Doubao web service, provided
for personal study, research and accessibility purposes only. It is not
affiliated with, authorized by, or endorsed by ByteDance or its affiliates.

Use of this software may violate the service's terms of use and may result in
rate limiting or account suspension. You are solely responsible for how you use
it and for any consequences. The software is provided "AS IS", without warranty
of any kind, and the authors accept no liability for any damages arising from
its use. Commercial use is prohibited. Third-party components remain under
their own licenses; BASS is free for non-commercial use only.

# StudyBuddy Integration Lab

组件组合试装配目录，不是正式产品，也不是参考资料目录。

用途：把已经在 `H:\studybuddy-composer` 独立测试通过的组件放在一起，验证它们之间的真实协作，例如：

- 文件导入 → 正文提取 → SQLite 保存；
- AI provider → 结构化模块生成 → 数据落库；
- RapidOCR → 文字结果 → 资料对象；
- whisper.cpp → 转写结果 → 资料对象；
- 报告生成 → QQ SMTP/飞书投递。

门禁：

- 未有 Composer 能力卡和独立通过结果的组件不得进入；
- 组合失败时退回 Composer 或重新定义 Adapter，不把失败代码移入主系统；
- Integration 代码不得被 `H:\studybuddy` import；
- 运行数据和凭据只放 `results/` 或 `H:\studybuddy-test`，且必须脱敏。

通过组合测试后，主系统仍需独立实现正式边界和用户路径验收。

# TikTok Shop 商品上架流水线

这是一个面向 Shopify／品牌商品页到 TikTok Shop 商品资料的 Codex Skill。它把产品证据、英文文案、图片翻译、最终排序、质量检查和飞书录入组织为可恢复、可验证的流水线。

## 核心能力

- 每轮处理 1～3 个产品，产品目录和 ChatGPT 对话相互隔离。
- 所有图片审计和视觉分析统一在 GPT 内置浏览器的产品专属 ChatGPT 对话中完成；本地脚本只校验文件、哈希和覆盖率。
- 图片翻译默认每批 5 张；失败项降级为 3+2，再降为单张。
- 最终图片按发布顺序和中文小标题命名。
- 区分目标变体、共享图片、其他变体和未知归属，避免混入错误 SKU 素材。
- 遇到超时或异常时先检查实际状态并定位根因，不重复盲试。
- 质量检查通过后才生成飞书录入载荷；外部写入仍需单一写入者和回读确认。

## 安装

将本仓库克隆到 Codex Skills 目录：

```powershell
git clone https://github.com/yugao-tum/tiktok-listing-pipeline.git "$HOME/.codex/skills/tiktok-listing-pipeline"
```

重新打开 Codex 任务后即可使用 `$tiktok-listing-pipeline`。

## 使用边界

Skill 不保存密码、Cookie、验证码或账号令牌，也不会在未明确授权时直接发布商品或写入外部系统。

详细流程见 [SKILL.md](SKILL.md)。

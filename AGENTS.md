## 项目说明

汇报只说三件事

1. 改了什么
2. 查了什么
3. 没查什么

## 提交规范

1. 禁止直接在 `master` 分支上提交；`master` 只接受通过 PR/MR 合入的变更。
2. 提交前先确认当前分支 `git branch --show-current`；输出为 `master` 时必须先新建分支再提交，不能直接 commit。
3. 没有正在进行的特性分支时，从最新 `master` 新建：`git switch -c <branch> origin/master`；已有特性分支时就在该分支上提交。
4. 分支命名使用 `feat/`、`fix/`、`chore/`、`docs/` 等前缀加简短英文主题。
5. 提交后工作区保持干净：`git status` 无残留改动。
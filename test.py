import feishu_bot; output, _ = feishu_bot.execute_shell(" crontab -l\); print(feishu_bot.build_card(\TEST\, output))

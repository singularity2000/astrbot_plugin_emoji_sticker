import asyncio
import random
import re
from typing import Final, List, Dict, Any, Optional

from astrbot.api import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Face, Image, Reply, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.provider import Provider

@register("astrbot_plugin_emoji_sticker", "Singularity2000", "QQ群贴表情监控插件", "1.0.0", "https://github.com/Singularity2000/astrbot_plugin_emoji_sticker")
class EmojiLikePlugin(Star):
    """
    贴表情插件

    特性：
    - 表情选用策略可配置
    - LLM 情感分析按需调用
    - 所有路径弱一致、可降级
    - 新增监控监控功能
    """

    # ---------- 1. 表情号段常量 ----------
    EMOJI_RANGE_START: Final[int] = 1  # 范围起点
    EMOJI_RANGE_END: Final[int] = 434  # 范围终点（不含）

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 情感映射
        self.emotions_mapping: dict[str, list[int]] = self.parse_emotions_mapping_list(
            self.config.get("emotions_mapping", [])
        )
        self.emotion_keywords: list[str] = list(self.emotions_mapping.keys())
        # 表情池
        self.emoji_pool = list(range(self.EMOJI_RANGE_START, self.EMOJI_RANGE_END))

    @staticmethod
    def parse_emotions_mapping_list(
        emotions_list: list[str],
    ) -> dict[str, list[int]]:
        """
        ["开心：1 2 3", "愤怒：4 5"] -> {"开心": [1,2,3], "愤怒": [4,5]}
        """
        result: dict[str, list[int]] = {}
        for item in emotions_list:
            try:
                emotion, values = item.split("：", 1)
                result[emotion.strip()] = list(map(int, values.split()))
            except Exception:
                logger.warning(f"无法解析情感映射项: {item}")
        return result

    def select_emoji_ids(
        self,
        *,
        emotion: str | None,
        need: int,
    ) -> list[int]:
        """
        表情选用策略入口
        """
        strategy = self.config.get("emoji_select_strategy", "random")

        if strategy == "random":
            return self._select_random(need)

        if strategy == "emotion_llm":
            return self._select_by_emotion(emotion, need)

        logger.warning(f"未知表情策略: {strategy}, 回退 random")
        return self._select_random(need)

    def _select_random(self, need: int) -> list[int]:
        return random.sample(self.emoji_pool, k=min(need, len(self.emoji_pool)))

    def _select_by_emotion(
        self,
        emotion: str | None,
        need: int,
    ) -> list[int]:
        if not emotion:
            return self._select_random(need)

        for keyword in self.emotion_keywords:
            if keyword in emotion:
                pool = self.emotions_mapping.get(keyword)
                if pool:
                    selected = random.sample(pool, k=min(need, len(pool)))
                    while len(selected) < need:
                        selected.append(random.choice(self.emoji_pool))
                    return selected

        return self._select_random(need)

    @filter.command("贴表情")
    async def replyMessage(
        self,
        event: AiocqhttpMessageEvent,
        emojiNum: Optional[int] = None,
    ):
        # 读取配置中的默认数量
        default_num = self.config.get("default_emoji_num", 1)
        if not isinstance(default_num, int) or default_num <= 0:
            default_num = 1
            
        if emojiNum is None:
            emojiNum = default_num
        else:
            try:
                emojiNum = int(emojiNum)
                if emojiNum <= 0:
                    emojiNum = 1
            except:
                emojiNum = 1

        chain = event.get_messages()
        if not chain:
            return

        reply = chain[0] if isinstance(chain[0], Reply) else None
        if not reply or not reply.chain:
            return

        text = reply.text
        message_id = reply.id
        images = [seg.url for seg in reply.chain if isinstance(seg, Image) and seg.url]

        if not text or not message_id:
            return

        emotion = None
        if self.config.get("emoji_select_strategy") == "emotion_llm":
            emotion = await self.judge_emotion(event, text, images)

        need = min(emojiNum, 20)
        emoji_ids = self.select_emoji_ids(
            emotion=emotion,
            need=need,
        )

        logger.info(f"贴表情: {emoji_ids}")

        for emoji_id in emoji_ids:
            await event.bot.set_msg_emoji_like(
                message_id=message_id,
                emoji_id=emoji_id,
                set=True,
            )
            await asyncio.sleep(self.config.get("emoji_interval", 0.5))

        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AiocqhttpMessageEvent):
        """群消息监听"""
        chain = event.get_messages()
        if not chain:
            return

        message_str = event.get_message_str()
        if not message_str:
            return

        if event.is_at_or_wake_command:
            return

        # 跟随已有表情
        face_segs = [seg for seg in chain if isinstance(seg, Face)]
        if face_segs and random.random() < self.config.get("emoji_follow", 0):
            face = random.choice(face_segs)
            try:
                await event.bot.set_msg_emoji_like(
                    message_id=event.message_obj.message_id,
                    emoji_id=face.id,
                    set=True,
                )
            except Exception as e:
                logger.warning(f"表情跟随失败: {e}")

        # 主动表情
        if random.random() < self.config.get("emoji_like_prob", 0):
            emotion = None
            if self.config.get("emoji_select_strategy") == "emotion_llm":
                emotion = await self.judge_emotion(event, message_str)

            emoji_ids = self.select_emoji_ids(
                emotion=emotion,
                need=1,
            )
            if not emoji_ids:
                return

            try:
                await event.bot.set_msg_emoji_like(
                    message_id=event.message_obj.message_id,
                    emoji_id=emoji_ids[0],
                    set=True,
                )
            except Exception as e:
                logger.warning(f"设置表情失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice(self, event: AiocqhttpMessageEvent):
        """监听通知事件 (贴表情)"""
        raw_event = event.message_obj.raw_message
        logger.debug(f"[QQ群贴表情监控插件] 收到原始事件数据: {raw_event}")
        if not isinstance(raw_event, dict):
            return
            
        post_type = raw_event.get("post_type")
        sub_type = raw_event.get("sub_type")
        notice_type = raw_event.get("notice_type")
        
        logger.debug(f"[QQ群贴表情监控插件] post_type={post_type}, notice_type={notice_type}, sub_type={sub_type}")
        
        if post_type != "notice":
            return
            
        # NapCat / OneBot V11 贴表情通知适配
        # 常见 notice_type: 'notify' (且 sub_type='emoji_like') 或 'group_msg_emoji_like'
        is_emoji_like = False
        emoji_id = None
        is_set = True
        
        if notice_type == "notify" and sub_type == "emoji_like":
            is_emoji_like = True
            emoji_id = raw_event.get("emoji_id")
            is_set = raw_event.get("set", True)
        elif notice_type == "group_msg_emoji_like":
            is_emoji_like = True
            # group_msg_emoji_like 格式中可能是 likes 列表
            likes = raw_event.get("likes", [])
            if likes and isinstance(likes, list):
                emoji_id = likes[0].get("emoji_id")
            else:
                emoji_id = raw_event.get("emoji_id")
            is_set = raw_event.get("is_add", raw_event.get("set", True))

        if not is_emoji_like:
            return
            
        # 1. 获取基础信息
        user_id = str(raw_event.get("user_id"))
        group_id = str(raw_event.get("group_id"))
        message_id = str(raw_event.get("message_id"))
        
        logger.debug(f"[QQ群贴表情监控插件] 解析到: user_id={user_id}, group_id={group_id}, emoji_id={emoji_id}, is_set={is_set}")
        
        # 0. 检查取消贴表情的监控策略
        unmonitor_strategy = self.config.get("unmonitor_emoji_like_strategy", "关闭监控取消贴表情事件")
        if not is_set and unmonitor_strategy == "关闭监控取消贴表情事件":
            return

        # 监控机器人自身开关
        self_id = str(event.message_obj.self_id)
        if user_id == self_id and not self.config.get("monitor_self", False):
            logger.debug(f"[QQ群贴表情监控插件] 忽略机器人自身贴表情: {user_id}")
            return

        # 会话 SID
        current_session_sid = f"napcat:GroupMessage:{group_id}"
        
        # 2. 检查全局监控范围（黑白名单）
        blacklist = self.config.get("blacklist", [])
        whitelist = self.config.get("whitelist", [])
        
        if current_session_sid in blacklist:
            logger.debug(f"[QQ群贴表情监控插件] SID {current_session_sid} 在黑名单中")
            return
        if whitelist and current_session_sid not in whitelist:
            logger.debug(f"[QQ群贴表情监控插件] SID {current_session_sid} 不在白名单中")
            return

        # 3. 获取相关人员信息、群信息和被贴消息内容
        # 获取贴表情者的信息
        try:
            operator_info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
            nickname = operator_info.get("nickname", "未知")
            card = operator_info.get("card", "")
            # 日志始终显示完整信息：nickname (card) (user_id)
            full_operator_name = f"{nickname} ({card})" if card else nickname
            full_operator_info = f"{full_operator_name} ({user_id})"
            
            # 推送显示方式
            op_display_mode = self.config.get("operator_display_mode", "全部显示")
            if op_display_mode == "仅显示昵称和群名片":
                push_operator_info = full_operator_name
            elif op_display_mode == "仅显示QQ号":
                push_operator_info = user_id
            else: # 全部显示
                push_operator_info = full_operator_info
        except Exception as e:
            logger.error(f"[QQ群贴表情监控插件] 获取群成员信息失败: {e}")
            full_operator_info = push_operator_info = f"未知 ({user_id})"
        
        # 获取群信息
        try:
            group_info = await event.bot.get_group_info(group_id=int(group_id))
            group_name = group_info.get("group_name", "未知群聊")
            # 日志始终显示完整信息：“group_name” (group_id)
            full_group_info = f"“{group_name}” ({group_id})"
            
            # 推送显示方式
            group_display_mode = self.config.get("group_display_mode", "全部显示")
            if group_display_mode == "仅显示群名":
                push_group_info = f"“{group_name}”"
            elif group_display_mode == "仅显示群号":
                push_group_info = group_id
            else: # 全部显示
                push_group_info = full_group_info
        except Exception as e:
            logger.error(f"[QQ群贴表情监控插件] 获取群信息失败: {e}")
            full_group_info = push_group_info = f"({group_id})"
        
        # 获取被贴消息内容
        try:
            msg_info = await event.bot.get_msg(message_id=message_id)
            msg_data = msg_info.get("message", [])
            content = ""
            if isinstance(msg_data, str):
                content = msg_data
            elif isinstance(msg_data, list):
                for seg in msg_data:
                    if seg.get("type") == "text":
                        content += seg.get("data", {}).get("text", "")
                    elif seg.get("type") == "face":
                        content += f"[表情{seg.get('data', {}).get('id')}]"
                    else:
                        content += f"[{seg.get('type')}]"
        except Exception as e:
            logger.error(f"[QQ群贴表情监控插件] 获取消息内容失败: {e}")
            content = "未知消息内容"
        
        # 4. 消息折叠处理
        fold_threshold = self.config.get("msg_fold_threshold", 0)
        if not isinstance(fold_threshold, int) or fold_threshold <= 0:
            fold_threshold = 0
            
        display_content = content
        if fold_threshold > 0 and len(content) > fold_threshold:
            display_content = content[:fold_threshold] + "……"

        # 5. 格式化监控日志和消息
        # 日志始终显示完整内容
        action_text = "贴了一个" if is_set else "撤回了贴表情"
        log_msg = f"{full_operator_info} 在 {full_group_info} 群中给消息“{display_content}”{action_text} [表情{emoji_id}]"
        logger.info(f"[QQ群贴表情监控插件] {log_msg}")

        # 6. 推送消息
        # 取消贴表情时，根据策略决定是否推送消息
        if not is_set and unmonitor_strategy == "在日志中推送":
            return
            
        # 推送时需要将 [表情id] 还原为 Face 组件，以便 QQ 原样显示
        push_list = self.config.get("push_list", [])
        logger.debug(f"[QQ群贴表情监控插件] 当前推送列表: {push_list}")
        for push_item in push_list:
            # 解析推送规则
            # 格式可能为：
            # 1. 推送目标SID (napcat:GroupMessage:12345678)
            # 2. 推送目标SID:来源SID1,来源SID2... (napcat:GroupMessage:78787878:56565656,12345678)
            
            # 使用正则匹配以正确处理包含多个冒号的 SID (platform:type:id)
            # 目标 SID 必定包含前三段，之后可能有冒号跟随来源列表
            match = re.match(r'^((?:[^:]+:){2}[^:]+)(?::(.*))?$', push_item)
            if not match:
                logger.debug(f"[QQ群贴表情监控插件] 推送项格式不匹配: {push_item}")
                continue
                
            target_sid = match.group(1)
            sources_part = match.group(2)
            
            should_push = False
            if not sources_part:
                # 全局推送：只要消息在全局范围内（通过了黑白名单过滤），就推送
                should_push = True
                logger.debug(f"[QQ群贴表情监控插件] 推送项 {push_item} 为全局推送，目标: {target_sid}")
            else:
                # 特定来源推送
                source_sids = [s.strip() for s in sources_part.split(",")]
                # 检查当前会话 SID 或纯数字群号是否在来源列表中
                if current_session_sid in source_sids or group_id in source_sids:
                    should_push = True
                    logger.debug(f"[QQ群贴表情监控插件] 推送项 {push_item} 匹配到来源: {current_session_sid}")
                else:
                    logger.debug(f"[QQ群贴表情监控插件] 推送项 {push_item} 未匹配到来源: {current_session_sid}")
            
            if should_push:
                chain = MessageChain()
                chain.chain.append(Plain(f"{push_operator_info} 在 {push_group_info} 群中对消息“{display_content}”{action_text}"))
                chain.chain.append(Face(id=int(emoji_id)))
                try:
                    await self.context.send_message(target_sid, chain)
                    logger.debug(f"[QQ群贴表情监控插件] 已发送推送至 {target_sid}")
                except Exception as e:
                    logger.error(f"[QQ群贴表情监控插件] 发送推送消息失败: {e}")

    async def judge_emotion(
        self,
        event: AiocqhttpMessageEvent,
        text: str,
        image_urls: list[str] | None = None,
    ) -> str:
        """LLM 情感判断"""
        system_prompt = (
            "你是一个情感分析专家，请判断文本情感，"
            f"只能从以下标签中选择一个：{self.emotion_keywords}"
        )
        prompt = f"文本内容：{text}"

        provider = self.context.get_provider_by_id(
            self.config["judge_provider_id"]
        ) or self.context.get_using_provider(event.unified_msg_origin)

        if not isinstance(provider, Provider):
            logger.error("未找到可用的 LLM Provider")
            return "其他"

        try:
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=prompt,
                image_urls=image_urls,
            )
            return resp.completion_text.strip()
        except Exception as e:
            logger.error(f"情感分析失败: {e}")
            return "其他"

"""弃船突袭处理器，处理弃船突袭特有的战斗结算逻辑。

与联盟沉船（CoalitionScuttle）一致：仅扣除进图时的一次 2 点心情，
非 S 评价及 C/D 战败不额外扣减心情。不再自动替换牺牲舰船，
战斗循环由 RunCount 等停止条件控制。
"""

from module.raid.combat import RaidCombat
from module.raid.run import RaidRun


class RaidScuttleCombat(RaidCombat):
    """共斗沉船复用公共结算，保留基础扣减并豁免额外沉船心情。"""

    combat_shipwreck_emotion = False


class RaidScuttleRun(RaidRun, RaidScuttleCombat):
    """弃船突袭主循环。

    与联盟沉船（CoalitionScuttle）一致：
    - 仅扣除进图时的一次 2 点心情，非 S 评价及战败不额外扣减 10 点心情
    - 不自动替换牺牲舰船，战斗循环由 RunCount 等停止条件控制
    """

    def handle_combat_low_emotion(self):
        """重写红脸出击警告弹窗处理。

        沉船任务中牺牲舰必然低心情，红脸弹窗出现时点击确认继续出击，
        不触发计算模式下的心情清零保底。

        Returns:
            bool: 是否处理了弹窗确认。
        """
        return self.handle_popup_confirm('IGNORE_LOW_EMOTION')

"""
自动兑换模块（Redeemer）

Polymarket 官方 py-clob-client SDK 不提供 redeem 接口（Issue #139 截至2026-02 仍 open）。
本模块使用社区库 polymarket-apis（pip install polymarket-apis）实现自动兑换：
  - 支持 EOA 钱包（signature_type=0，需 POL 支付 gas）
  - 通过 PolymarketDataClient.get_positions(redeemable=True) 查询可兑换仓位
  - 通过 PolymarketWeb3Client.redeem_position() 链上兑换

使用方式：
  from src.redeemer import AutoRedeemer
  redeemer = AutoRedeemer(private_key, wallet_address)
  redeemer.redeem_one(condition_id, outcome_index, size, neg_risk)
  # 或定期扫描全部可兑换仓位：
  redeemer.redeem_all()
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("redeemer")

# 延迟导入，运行时若未安装则优雅降级
try:
    from polymarket_apis import PolymarketWeb3Client, PolymarketDataClient
    _HAS_PM_APIS = True
except ImportError:
    _HAS_PM_APIS = False
    logger.warning("polymarket-apis 未安装，自动兑换功能不可用。运行: pip install polymarket-apis")


class RedeemResult:
    def __init__(self, condition_id: str, success: bool,
                 tx_hash: Optional[str] = None, error: Optional[str] = None,
                 amount: float = 0.0):
        self.condition_id = condition_id
        self.success      = success
        self.tx_hash      = tx_hash
        self.error        = error
        self.amount       = amount

    def __repr__(self):
        if self.success:
            return f"RedeemResult(✅ {self.condition_id[:10]}… tx={self.tx_hash} amt={self.amount:.2f})"
        return f"RedeemResult(❌ {self.condition_id[:10]}… err={self.error})"


class AutoRedeemer:
    """
    自动兑换器。

    参数：
      private_key    - EOA 私钥（与 bot 钱包相同）
      wallet_address - 钱包地址（用于查询仓位）
      signature_type - 0=EOA(默认), 1=Magic/Email, 2=Safe/Gnosis
      retry_times    - 每次兑换失败后重试次数
      retry_delay    - 重试间隔（秒）
    """

    def __init__(
        self,
        private_key: str,
        wallet_address: str,
        signature_type: int = 0,
        retry_times: int = 3,
        retry_delay: float = 5.0,
    ):
        self._available = _HAS_PM_APIS and bool(private_key) and bool(wallet_address)
        self._wallet    = wallet_address
        self._retry     = retry_times
        self._delay     = retry_delay

        if not _HAS_PM_APIS:
            return
        if not private_key:
            logger.warning("Redeemer: 未配置 PRIVATE_KEY，自动兑换不可用")
            self._available = False
            return

        try:
            self._web3 = PolymarketWeb3Client(
                private_key    = private_key,
                signature_type = signature_type,
            )
            self._data = PolymarketDataClient()
            logger.info("Redeemer 初始化成功 | 钱包=%s | 签名类型=%d",
                        wallet_address[:10] + "…", signature_type)
        except Exception as e:
            logger.error("Redeemer 初始化失败: %s", e)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ── 核心方法 ─────────────────────────────────

    def redeem_one(
        self,
        condition_id:  str,
        outcome_index: int,
        size:          float,
        neg_risk:      bool = False,
    ) -> RedeemResult:
        """
        兑换单个仓位。

        参数：
          condition_id  - 市场 condition_id
          outcome_index - 胜出结果的下标（0=Up/Yes, 1=Down/No）
          size          - 持仓数量（shares）
          neg_risk      - 是否为 neg_risk 市场（BTC 5m 通常为 False）
        """
        if not self._available:
            return RedeemResult(condition_id, False, error="Redeemer 不可用")

        # amounts 数组：胜出方填 size，败出方填 0
        amounts = [0.0, 0.0]
        if 0 <= outcome_index <= 1:
            amounts[outcome_index] = size
        else:
            return RedeemResult(condition_id, False, error=f"无效 outcome_index={outcome_index}")

        last_error = ""
        for attempt in range(1, self._retry + 1):
            try:
                logger.info("兑换 %s | 下标=%d 数量=%.4f neg_risk=%s (第%d次)",
                            condition_id[:12] + "…", outcome_index, size, neg_risk, attempt)
                receipt = self._web3.redeem_position(
                    condition_id = condition_id,
                    amounts      = amounts,
                    neg_risk     = neg_risk,
                )
                tx_hash = (receipt.transactionHash.hex()
                           if hasattr(receipt, "transactionHash") else str(receipt))
                logger.info("✅ 兑换成功 tx=%s", tx_hash[:16] + "…")
                return RedeemResult(condition_id, True, tx_hash=tx_hash, amount=size)

            except Exception as e:
                last_error = str(e)
                logger.warning("兑换失败(第%d次): %s", attempt, last_error)
                if attempt < self._retry:
                    time.sleep(self._delay * attempt)  # 指数退避

        return RedeemResult(condition_id, False, error=last_error)

    def redeem_all(self, size_threshold: float = 0.01) -> list[RedeemResult]:
        """
        扫描钱包内所有可兑换仓位并批量兑换。
        一般在结算后调用，或作为定期后台任务运行。

        参数：
          size_threshold - 最小仓位数量（过滤尘埃仓位）
        """
        if not self._available:
            logger.warning("Redeemer 不可用，跳过批量兑换")
            return []

        try:
            positions = self._data.get_positions(
                user           = self._wallet,
                redeemable     = True,
                size_threshold = size_threshold,
            )
        except Exception as e:
            logger.error("查询可兑换仓位失败: %s", e)
            return []

        if not positions:
            logger.debug("无可兑换仓位")
            return []

        logger.info("发现 %d 个可兑换仓位", len(positions))
        results = []
        for pos in positions:
            result = self.redeem_one(
                condition_id  = pos.condition_id,
                outcome_index = pos.outcome_index,
                size          = pos.size,
                neg_risk      = pos.negative_risk,
            )
            results.append(result)
            if result.success:
                logger.info("✅ 兑换成功: %s | 数量=%.4f", pos.title[:30], pos.size)
            else:
                logger.error("❌ 兑换失败: %s | 错误=%s", pos.title[:30], result.error)
            # 兑换之间稍作等待，避免触发链上 nonce 冲突
            time.sleep(1.0)

        wins = sum(1 for r in results if r.success)
        logger.info("批量兑换完成: %d/%d 成功", wins, len(results))
        return results

    def get_redeemable_positions(self, size_threshold: float = 0.01):
        """查询但不兑换，用于展示/日志。"""
        if not self._available:
            return []
        try:
            return self._data.get_positions(
                user           = self._wallet,
                redeemable     = True,
                size_threshold = size_threshold,
            )
        except Exception as e:
            logger.error("查询可兑换仓位失败: %s", e)
            return []

#!/usr/bin/env python3
"""测试健身房 Agent + 数据库集成

本测试验证 MiniMax Agent 能否正确理解自然语言并调用数据库函数，
完成健身房的日常管理任务。
"""
import os
import sys
import asyncio
import pytest
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from agent import Agent, create_provider
from agent.functions.registry import FunctionRegistry
from database import DatabaseManager
from database.models import ServiceRecord, Membership, ProductSale

# 导入业务函数
# examples/ 目录已随美业转型移除（gym_agent_manager 是健身房遗留示例），
# 模块缺失时跳过整个文件，避免 CI collection error
if not (project_root / "examples" / "gym_agent_manager.py").exists():
    pytest.skip("examples/gym_agent_manager.py 已随美业转型移除", allow_module_level=True)
sys.path.insert(0, str(project_root / "examples"))
from gym_agent_manager import (
    record_service_income,
    open_membership_card,
    record_product_sale,
    query_daily_income,
    query_member_info,
    query_trainer_commission,
    init_database
)


@pytest.fixture(scope="module")
def test_database():
    """创建测试数据库"""
    import gym_agent_manager
    
    # 使用内存数据库进行测试
    data_dir = project_root / "data"
    data_dir.mkdir(exist_ok=True)
    
    db_path = data_dir / "test_gym_agent.db"
    if db_path.exists():
        db_path.unlink()
    
    db_url = f"sqlite:///{db_path}"
    
    repo = DatabaseManager(database_url=db_url)
    repo.create_tables()
    
    # 设置全局仓库
    gym_agent_manager.repo = repo
    
    # 初始化基础数据
    gym_agent_manager._init_base_data()
    
    yield repo
    
    # 清理
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def function_registry():
    """创建函数注册表"""
    registry = FunctionRegistry()
    registry.register("record_service_income", "记录服务收入", record_service_income)
    registry.register("open_membership_card", "开会员卡", open_membership_card)
    registry.register("record_product_sale", "记录商品销售", record_product_sale)
    registry.register("query_daily_income", "查询每日收入", query_daily_income)
    registry.register("query_member_info", "查询会员信息", query_member_info)
    registry.register("query_trainer_commission", "查询私教提成", query_trainer_commission)
    return registry


@pytest.fixture
async def gym_agent(function_registry):
    """创建健身房管理 Agent"""
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        pytest.skip("未设置 MINIMAX_API_KEY 环境变量")
    
    provider = create_provider(
        "minimax",
        api_key=api_key,
        model="MiniMax-M2.5"
    )
    
    agent = Agent(
        provider,
        function_registry=function_registry,
        system_prompt="""你是健身房的智能管理助手。你能帮助健身房经营者：
1. 记录每日收入（私教课、团课、会员卡、商品销售）
2. 自动计算私教提成（私教课提成40%）
3. 查询统计数据

规则：
- 私教课程提成40%，团课无提成
- 认真理解用户的自然语言输入，准确调用相应的工具"""
    )
    
    return agent


class TestGymAgentIntegration:
    """健身房 Agent 集成测试"""
    
    @pytest.mark.asyncio
    async def test_record_private_training(self, gym_agent, test_database):
        """测试记录私教课程"""
        print("\n" + "="*60)
        print("测试 1: 记录私教课程")
        print("="*60)
        
        user_input = "今天张三上了李教练的私教课，收费300元"
        print(f"用户输入: {user_input}")
        
        response = await gym_agent.chat(user_input, temperature=0.1)
        
        print(f"Agent 回复: {response['content']}")
        print(f"调用的工具: {[fc['name'] for fc in response['function_calls']]}")
        
        # 验证
        assert len(response['function_calls']) > 0, "应该调用了工具函数"
        assert any(fc['name'] == 'record_service_income' for fc in response['function_calls']), \
            "应该调用了 record_service_income 函数"
        
        # 验证数据库记录
        with test_database.get_session() as session:
            record = session.query(ServiceRecord).filter(
                ServiceRecord.customer_id.isnot(None)
            ).first()
            
            assert record is not None, "应该创建了服务记录"
            assert record.customer.name == "张三", "顾客名称应该是张三"
            assert float(record.amount) == 300.0, "金额应该是300"
            assert float(record.commission_amount) == 120.0, "私教提成应该是120（40%）"
        
        print("✅ 私教课程记录测试通过")
    
    @pytest.mark.asyncio
    async def test_open_membership(self, gym_agent, test_database):
        """测试开会员卡"""
        print("\n" + "="*60)
        print("测试 2: 开会员卡")
        print("="*60)
        
        gym_agent.clear_history()  # 清除历史
        
        user_input = "李四开了一张年卡，充值3000元"
        print(f"用户输入: {user_input}")
        
        response = await gym_agent.chat(user_input, temperature=0.1)
        
        print(f"Agent 回复: {response['content']}")
        print(f"调用的工具: {[fc['name'] for fc in response['function_calls']]}")
        
        # 验证
        assert len(response['function_calls']) > 0, "应该调用了工具函数"
        assert any(fc['name'] == 'open_membership_card' for fc in response['function_calls']), \
            "应该调用了 open_membership_card 函数"
        
        # 验证数据库记录
        with test_database.get_session() as session:
            membership = session.query(Membership).filter(
                Membership.customer_id.isnot(None)
            ).first()
            
            assert membership is not None, "应该创建了会员卡"
            assert membership.customer.name == "李四", "顾客名称应该是李四"
            assert float(membership.total_amount) == 3000.0, "充值金额应该是3000"
            assert membership.points == 300, "积分应该是300（每10元1积分）"
        
        print("✅ 开会员卡测试通过")
    
    @pytest.mark.asyncio
    async def test_record_product_sale(self, gym_agent, test_database):
        """测试记录商品销售"""
        print("\n" + "="*60)
        print("测试 3: 记录商品销售")
        print("="*60)
        
        gym_agent.clear_history()
        
        user_input = "王五买了一瓶蛋白粉，200元"
        print(f"用户输入: {user_input}")
        
        response = await gym_agent.chat(user_input, temperature=0.1)
        
        print(f"Agent 回复: {response['content']}")
        print(f"调用的工具: {[fc['name'] for fc in response['function_calls']]}")
        
        # 验证
        assert len(response['function_calls']) > 0, "应该调用了工具函数"
        assert any(fc['name'] == 'record_product_sale' for fc in response['function_calls']), \
            "应该调用了 record_product_sale 函数"
        
        # 验证数据库记录
        with test_database.get_session() as session:
            sale = session.query(ProductSale).filter(
                ProductSale.customer_id.isnot(None)
            ).first()
            
            assert sale is not None, "应该创建了销售记录"
            assert sale.customer.name == "王五", "顾客名称应该是王五"
            assert float(sale.total_amount) == 200.0, "金额应该是200"
        
        print("✅ 商品销售记录测试通过")
    
    @pytest.mark.asyncio
    async def test_query_daily_income(self, gym_agent, test_database):
        """测试查询每日收入"""
        print("\n" + "="*60)
        print("测试 4: 查询每日收入")
        print("="*60)
        
        gym_agent.clear_history()
        
        user_input = "查询一下今天的收入情况"
        print(f"用户输入: {user_input}")
        
        response = await gym_agent.chat(user_input, temperature=0.1)
        
        print(f"Agent 回复: {response['content']}")
        print(f"调用的工具: {[fc['name'] for fc in response['function_calls']]}")
        
        # 验证
        assert len(response['function_calls']) > 0, "应该调用了工具函数"
        assert any(fc['name'] == 'query_daily_income' for fc in response['function_calls']), \
            "应该调用了 query_daily_income 函数"
        
        # 验证回复包含收入信息
        assert "收入" in response['content'] or "元" in response['content'], \
            "回复应该包含收入信息"
        
        print("✅ 查询收入测试通过")
    
    @pytest.mark.asyncio
    async def test_query_member_info(self, gym_agent, test_database):
        """测试查询会员信息"""
        print("\n" + "="*60)
        print("测试 5: 查询会员信息")
        print("="*60)
        
        gym_agent.clear_history()
        
        user_input = "查一下李四的会员信息"
        print(f"用户输入: {user_input}")
        
        response = await gym_agent.chat(user_input, temperature=0.1)
        
        print(f"Agent 回复: {response['content']}")
        print(f"调用的工具: {[fc['name'] for fc in response['function_calls']]}")
        
        # 验证
        assert len(response['function_calls']) > 0, "应该调用了工具函数"
        assert any(fc['name'] == 'query_member_info' for fc in response['function_calls']), \
            "应该调用了 query_member_info 函数"
        
        # 验证回复包含会员信息
        assert "李四" in response['content'], "回复应该包含会员姓名"
        
        print("✅ 查询会员信息测试通过")
    
    @pytest.mark.asyncio
    async def test_query_trainer_commission(self, gym_agent, test_database):
        """测试查询私教提成"""
        print("\n" + "="*60)
        print("测试 6: 查询私教提成")
        print("="*60)
        
        gym_agent.clear_history()
        
        user_input = "统计一下李教练今天的提成"
        print(f"用户输入: {user_input}")
        
        response = await gym_agent.chat(user_input, temperature=0.1)
        
        print(f"Agent 回复: {response['content']}")
        print(f"调用的工具: {[fc['name'] for fc in response['function_calls']]}")
        
        # 验证
        assert len(response['function_calls']) > 0, "应该调用了工具函数"
        assert any(fc['name'] == 'query_trainer_commission' for fc in response['function_calls']), \
            "应该调用了 query_trainer_commission 函数"
        
        # 验证回复包含提成信息
        assert "李教练" in response['content'] or "提成" in response['content'], \
            "回复应该包含提成信息"
        
        print("✅ 查询提成测试通过")
    
    @pytest.mark.asyncio
    async def test_complex_scenario(self, gym_agent, test_database):
        """测试复杂场景（多轮对话）"""
        print("\n" + "="*60)
        print("测试 7: 复杂场景（多轮对话）")
        print("="*60)
        
        gym_agent.clear_history()
        
        # 场景1: 记录多个服务
        print("\n[轮次 1] 记录服务")
        response1 = await gym_agent.chat(
            "今天有两个人上了私教课：赵六300元，钱七300元，都是李教练带的",
            temperature=0.1
        )
        print(f"Agent: {response1['content']}")
        
        # 场景2: 查询汇总
        print("\n[轮次 2] 查询汇总")
        response2 = await gym_agent.chat(
            "那李教练今天一共能拿多少提成？",
            temperature=0.1
        )
        print(f"Agent: {response2['content']}")
        
        # 验证
        assert "提成" in response2['content'], "应该回答了提成问题"
        
        print("✅ 复杂场景测试通过")


async def main():
    """主测试函数"""
    print("="*60)
    print("健身房 Agent + 数据库集成测试")
    print("="*60)
    print()
    
    # 检查 API Key
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        print("❌ 错误: 未设置 MINIMAX_API_KEY 环境变量")
        print("\n使用方法:")
        print("  export MINIMAX_API_KEY='your-api-key'")
        print("  python tests/agent/test_gym_agent_integration.py")
        return
    
    # 初始化测试数据库
    import gym_agent_manager
    
    data_dir = project_root / "data"
    data_dir.mkdir(exist_ok=True)
    
    db_path = data_dir / "test_gym_agent.db"
    if db_path.exists():
        db_path.unlink()
    
    db_url = f"sqlite:///{db_path}"
    repo = DatabaseManager(database_url=db_url)
    repo.create_tables()
    gym_agent_manager.repo = repo
    gym_agent_manager._init_base_data()
    
    # 创建函数注册表
    registry = FunctionRegistry()
    registry.register("record_service_income", "记录服务收入", record_service_income)
    registry.register("open_membership_card", "开会员卡", open_membership_card)
    registry.register("record_product_sale", "记录商品销售", record_product_sale)
    registry.register("query_daily_income", "查询每日收入", query_daily_income)
    registry.register("query_member_info", "查询会员信息", query_member_info)
    registry.register("query_trainer_commission", "查询私教提成", query_trainer_commission)
    
    # 创建 Agent
    provider = create_provider(
        "minimax",
        api_key=api_key,
        model="MiniMax-M2.5"
    )
    
    agent = Agent(
        provider,
        function_registry=registry,
        system_prompt="""你是健身房的智能管理助手。你能帮助健身房经营者：
1. 记录每日收入（私教课、团课、会员卡、商品销售）
2. 自动计算私教提成（私教课提成40%）
3. 查询统计数据

规则：
- 私教课程提成40%，团课无提成
- 认真理解用户的自然语言输入，准确调用相应的工具"""
    )
    
    # 运行测试
    test_instance = TestGymAgentIntegration()
    
    tests = [
        ("记录私教课程", test_instance.test_record_private_training),
        ("开会员卡", test_instance.test_open_membership),
        ("记录商品销售", test_instance.test_record_product_sale),
        ("查询每日收入", test_instance.test_query_daily_income),
        ("查询会员信息", test_instance.test_query_member_info),
        ("查询私教提成", test_instance.test_query_trainer_commission),
        ("复杂场景", test_instance.test_complex_scenario),
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        try:
            await test_func(agent, repo)
            passed += 1
        except Exception as e:
            print(f"\n❌ 测试失败: {test_name}")
            print(f"错误: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    # 打印总结
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    print(f"通过: {passed}/{len(tests)}")
    print(f"失败: {failed}/{len(tests)}")
    
    if failed == 0:
        print("\n🎉 所有测试通过！")
    else:
        print(f"\n⚠️  有 {failed} 个测试失败")
    
    # 清理
    if db_path.exists():
        db_path.unlink()
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)


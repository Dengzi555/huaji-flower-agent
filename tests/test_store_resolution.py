"""门店实体解析契约：目录扩展、范围、否定、权限与模型建议均不能悄悄查错店。"""
import unittest
from ecommerce_agent.store_resolution import StoreDirectory,resolve_stores

SHOPS=[
    {'id':'a','name':'杭州湖滨店','city':'杭州','aliases':['湖滨店','湖滨']},
    {'id':'b','name':'杭州湖滨二店','city':'杭州','aliases':['湖滨二店']},
    {'id':'c','name':'南京湖滨店','city':'南京','aliases':['湖滨店']},
    {'id':'d','name':'南京新街口店','city':'南京','aliases':['新街口店']},
]


class StoreResolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory=StoreDirectory(SHOPS)
        self.context={'focus_store':'a','query_store_ids':['a'],'recent_store_ids':['a','c'],'revision':3}

    def resolve(self,text,**kwargs):return resolve_stores(text,self.directory,self.context,**kwargs)

    def test_new_city_and_full_name_are_configuration_only(self):
        result=self.resolve('南京新街口店昨天销量')
        self.assertEqual(result['store_ids'],['d']);self.assertEqual(result['status'],'resolved')
        self.assertEqual(result['model_calls'],0)

    def test_shared_alias_is_ambiguous_not_a_comparison(self):
        result=self.resolve('湖滨店昨天卖了多少')
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(result['candidate_ids'],['a','c']);self.assertEqual(result['store_ids'],[])

    def test_city_qualifies_ambiguous_alias_without_guess(self):
        self.assertEqual(self.resolve('南京的湖滨店昨天销量')['store_ids'],['c'])
        self.assertEqual(self.resolve('杭州的湖滨店昨天销量')['store_ids'],['a'])

    def test_city_in_nonbusiness_question_is_not_a_shop_selection(self):
        result=self.resolve('杭州今天下雨吗')
        self.assertEqual(result['source'],'context');self.assertEqual(result['store_ids'],['a'])

    def test_all_store_quantifier_forms_are_not_unknown_store_names(self):
        for phrase in ('所有的店都看下','全部的门店','各个店铺','每一家店'):
            with self.subTest(phrase=phrase):
                result=self.resolve('今年情人节哪款卖的最好，'+phrase)
                self.assertEqual(result['status'],'resolved',result)
                self.assertEqual(set(result['store_ids']),{'a','b','c','d'})
                self.assertEqual(result['action'],'compare')

    def test_city_only_business_followup_can_clarify_store_scope(self):
        result=self.resolve('杭州呢')
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(result['candidate_ids'],['a','b'])

    def test_initial_current_shop_pronoun_uses_selected_shop(self):
        context={**self.context,'query_store_ids':[]}
        self.assertEqual(resolve_stores('这家店昨天怎么样',self.directory,context)['store_ids'],['a'])

    def test_longest_name_does_not_match_shorter_store(self):
        self.assertEqual(self.resolve('杭州湖滨二店昨天销量')['store_ids'],['b'])

    def test_explicit_comparison_and_two_mentions_differ(self):
        self.assertEqual(self.resolve('杭州湖滨店和南京新街口店对比')['action'],'compare')
        self.assertEqual(self.resolve('杭州湖滨店店长提到了南京新街口店')['status'],'needs_clarification')

    def test_exclusion_and_correction_are_not_comparison(self):
        for text in ('不看杭州湖滨店，看看南京新街口店','不是杭州湖滨店，是南京新街口店'):
            result=self.resolve(text)
            self.assertEqual(result['store_ids'],['d']);self.assertEqual(result['excluded_store_ids'],['a'])
            self.assertEqual(result['action'],'query')

    def test_unknown_store_never_falls_back_even_with_known_store(self):
        for text in ('北京店昨天销量','杭州湖滨店和北京店对比','北京店全店昨天销量'):
            result=self.resolve(text);self.assertEqual(result['status'],'needs_clarification')
            self.assertEqual(result['store_ids'],[])

    def test_city_group_uses_directory_and_no_city_group_requires_clarification(self):
        self.assertEqual(self.resolve('杭州所有店对比')['store_ids'],['a','b'])
        self.assertEqual(self.resolve('杭州昨天销量')['status'],'needs_clarification')

    def test_other_stores_excludes_anchor_and_reference_keeps_pair(self):
        result=self.resolve('其他店呢')
        self.assertEqual(set(result['store_ids']),{'b','c','d'})
        context={**self.context,'comparison_store_ids':['a','d']}
        self.assertEqual(resolve_stores('这两家库存呢',self.directory,context)['store_ids'],['a','d'])

    def test_explicit_exclusion_never_reintroduces_anchor_as_reference(self):
        result=self.resolve('杭州湖滨店除外，其他店呢')
        self.assertNotIn('a',result['store_ids']+result['reference_store_ids'])

    def test_unique_store_pronoun_and_product_pronoun(self):
        self.assertEqual(self.resolve('这家店呢')['store_ids'],['a'])
        self.assertEqual(self.resolve('它的销量呢')['source'],'context')
        context={**self.context,'query_store_ids':['a','c']}
        self.assertEqual(resolve_stores('那家店呢',self.directory,context)['status'],'needs_clarification')

    def test_switch_action_is_not_a_data_query(self):
        result=self.resolve('切回南京新街口店')
        self.assertEqual(result['action'],'switch');self.assertEqual(result['store_ids'],['d'])

    def test_allowed_scope_is_checked_even_for_model(self):
        result=self.resolve('南京新街口店昨天销量',allowed_ids=['a'])
        self.assertEqual(result['status'],'forbidden');self.assertEqual(result['store_ids'],[])
        self.assertEqual(result['candidate_ids'],[])

    def test_model_handles_roles_but_must_ground_selected_ids(self):
        seen=[]
        def planner(text,ctx):
            seen.append(ctx)
            return {'decision':'select','action':'query','store_ids':['d'],'excluded_store_ids':[]}
        result=self.resolve('杭州湖滨店店长说想看南京新街口店的情况',planner=planner)
        self.assertEqual(result['store_ids'],['d']);self.assertEqual(result['source'],'model_validated')
        self.assertEqual(result['context_revision'],3);self.assertEqual(len(seen),1)
        bad=lambda *a:{'decision':'select','action':'query','store_ids':['not-real'],'excluded_store_ids':[]}
        self.assertEqual(self.resolve('杭州湖滨店说南京新街口店',planner=bad)['status'],'failed')

    def test_longtail_collection_reference_uses_one_model_plan_with_frozen_candidates(self):
        context={**self.context,'query_store_ids':['a','d'],'comparison_store_ids':['a','d']};calls=[]
        def planner(q,ctx):
            calls.append(ctx)
            return dict(decision='select',action='compare',store_ids=['a','d'],excluded_store_ids=[])
        result=resolve_stores('刚才这几个店都看看',self.directory,context,planner=planner)
        self.assertEqual(result['source'],'model_validated');self.assertEqual(result['store_ids'],['a','d'])
        self.assertEqual(len(calls),1);self.assertEqual({s['id'] for s in calls[0]['stores']},{'a','d'})

    def test_unlisted_narrative_role_still_uses_model_instead_of_comparing_all_mentions(self):
        seen=[]
        def planner(q,ctx):
            seen.append(ctx)
            return dict(decision='select',action='compare',store_ids=['c','d'],excluded_store_ids=[])
        result=self.resolve('杭州湖滨店经理问我南京湖滨店和南京新街口店哪家好',planner=planner)
        self.assertEqual(result['store_ids'],['c','d']);self.assertEqual(len(seen),1)

    def test_model_cannot_resolve_shared_alias_by_guess(self):
        def forbidden(*a):raise AssertionError('同名分店缺少区分证据，模型也不能猜')
        self.assertEqual(self.resolve('湖滨店昨天销量',planner=forbidden)['status'],'needs_clarification')

    def test_model_failure_and_explicit_unknown_do_not_query_current_store(self):
        def failing(*a):raise TimeoutError()
        self.assertEqual(self.resolve('杭州湖滨店提到南京新街口店',planner=failing)['status'],'failed')
        self.assertEqual(self.resolve('北京店昨天销量',planner=failing)['status'],'needs_clarification')

    def test_catalog_validation_and_version(self):
        with self.assertRaises(ValueError):StoreDirectory([SHOPS[0],SHOPS[0]])
        self.assertNotEqual(self.directory.version,StoreDirectory([{**p,'aliases':[]} for p in SHOPS]).version)


if __name__=='__main__':unittest.main()

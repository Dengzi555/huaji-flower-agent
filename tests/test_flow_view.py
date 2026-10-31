"""流程页清单由源码生成；检测修改，且不读取密钥。"""
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree
from ecommerce_agent.flow_view import FlowView,TRACKED,render_svg


class FlowViewTests(unittest.TestCase):
    """修改隔离的源码副本验证自动刷新，无需改动业务输入或执行模型。"""
    def test_source_change_updates_routes_and_requires_restart(self):
        origin=Path(__file__).resolve().parents[1]/'ecommerce_agent'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);code=root/'ecommerce_agent';code.mkdir()
            for name in TRACKED:shutil.copy2(origin/name,code/name)
            (root/'.env').write_text('must-not-read-secret',encoding='utf-8')
            view=FlowView(root);first=view.snapshot()
            self.assertFalse(first['restart_required']);self.assertEqual(len(first['routes']),4)
            self.assertIn('finish',{t['name'] for t in first['tools']})
            path=code/'routing.py';path.write_text(path.read_text(encoding='utf-8').replace('销量表现与趋势','单日销售概览'),encoding='utf-8')
            second=view.snapshot()
            self.assertTrue(second['restart_required']);self.assertNotEqual(first['version'],second['version'])
            self.assertEqual(second['routes'][0]['label'],'单日销售概览')
            self.assertNotIn('must-not-read-secret',str(second))
            svg=ElementTree.fromstring(second['svg']);self.assertEqual(svg.attrib['viewBox'],'0 0 1480 2040')
            self.assertEqual(second['stages'][0]['stage'],'输入')
            self.assertNotIn('用户手动选择',str(second['stages']))

    def test_main_path_and_planning_boundary(self):
        """检查业务关键连线，防止图再次把未命中变追问、或把规划放在执行之后。"""
        svg=ElementTree.fromstring(render_svg())
        nodes={n.attrib['data-node-id'] for n in svg.iter() if 'data-node-id' in n.attrib}
        edges={(n.attrib['data-from'],n.attrib['data-to']) for n in svg.iter() if 'data-from' in n.attrib}
        expected={('input','stores'),('stores','rules'),('stores','chat'),
                  ('rules','retrieval'),('retrieval','planner'),('planner','validate'),
                  ('rules','validate'),('validate','clarify'),('validate','errors'),
                  ('validate','queue'),('queue','lookup'),('queue','investigate'),
                  ('lookup','facts'),('investigate','facts'),('facts','output')}
        self.assertTrue(expected <= edges)
        self.assertTrue(all(a in nodes and b in nodes for a,b in edges))
        self.assertNotIn(('planner','rules'),edges)
        self.assertNotIn(('planner','investigate'),edges)
        self.assertFalse(any(a=='evaluation' or b=='evaluation' for a,b in edges))
        content=''.join(svg.itertext())
        for phrase in ['新查询未提门店 → 全部可访问门店','哪个店最好','各店哪款最好','缺数据不当作零']:
            self.assertIn(phrase,content)

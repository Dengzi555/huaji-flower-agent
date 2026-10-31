"""有界CSV/XLSX读写：不执行公式、不启动Excel、不解压文件到磁盘。"""
import csv
import io
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

MAX_BYTES=2_000_000
MAX_ROWS=5000
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def xml(raw):
    """拒绝DTD/实体声明，压缩包大小在解析前检查。"""
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('Excel不允许DTD或实体声明。')
    return ET.fromstring(raw)


def read_table(filename, raw, sheet_index=0,header_row=1):
    """返回表头、带原文件行号的数据和工作表名；支持UTF-8/GB18030 CSV及无公式xlsx。"""
    if not raw or len(raw)>MAX_BYTES:raise ValueError('文件需在1字节至2MB之间。')
    if type(header_row) is not int or not 1<=header_row<=30:raise ValueError('表头行号需为1–30。')
    sheets=[]
    if filename.lower().endswith('.csv'):
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:text=raw.decode('gb18030')
            except UnicodeDecodeError:raise ValueError('CSV需为UTF-8或GB18030编码。') from None
        try:dialect=csv.Sniffer().sniff(text[:8000],delimiters=',;\t')
        except csv.Error:dialect=csv.excel
        try:rows=list(csv.reader(io.StringIO(text),dialect))
        except csv.Error:raise ValueError('CSV格式无法读取。') from None
    elif filename.lower().endswith('.xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                infos=z.infolist()
                if len(infos)>300 or sum(i.file_size for i in infos)>20_000_000:raise ValueError('Excel展开后过大。')
                if any('vbaProject' in i.filename or 'externalLinks/' in i.filename for i in infos):raise ValueError('不接收宏或外部链接。')
                wb=xml(z.read('xl/workbook.xml'));rels=xml(z.read('xl/_rels/workbook.xml.rels'))
                links={r.attrib['Id']:r.attrib['Target'] for r in rels if r.attrib.get('TargetMode')!='External'}
                sheet_nodes=wb.findall('s:sheets/s:sheet',NS);sheets=[s.attrib['name'] for s in sheet_nodes]
                if type(sheet_index) is not int or not 0<=sheet_index<len(sheets):raise ValueError('工作表序号不合法。')
                rid=sheet_nodes[sheet_index].attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
                target=links[rid];path=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
                if not path.startswith('xl/worksheets/'):raise ValueError('工作表路径不合法。')
                strings=[]
                if 'xl/sharedStrings.xml' in z.namelist():
                    strings=[''.join(si.itertext()) for si in xml(z.read('xl/sharedStrings.xml')).findall('s:si',NS)]
                date_styles=set()
                if 'xl/styles.xml' in z.namelist():
                    styles=xml(z.read('xl/styles.xml'));formats={int(n.attrib['numFmtId']):n.attrib['formatCode'] for n in styles.findall('s:numFmts/s:numFmt',NS)}
                    for n,xf in enumerate(styles.findall('s:cellXfs/s:xf',NS)):
                        fmt=int(xf.attrib.get('numFmtId',0));code=re.sub(r'"[^"]*"|\[[^\]]*\]','',formats.get(fmt,''))
                        if 14<=fmt<=22 or re.search('[ymd]',code,re.I):date_styles.add(n)
                props=wb.find('s:workbookPr',NS);epoch=datetime(1904,1,1) if props is not None and props.attrib.get('date1904') in ('1','true') else datetime(1899,12,30)
                rows=[]
                for row in xml(z.read(path)).findall('s:sheetData/s:row',NS):
                    cells=[]
                    for cell in row.findall('s:c',NS):
                        if cell.find('s:f',NS) is not None:raise ValueError('文件含公式，请先在Excel另存为数值后上传。')
                        ref=cell.attrib.get('r','');letters=re.match('[A-Z]+',ref)
                        if not letters:raise ValueError('Excel单元格地址缺失。')
                        column=0
                        for char in letters[0]:column=column*26+ord(char)-64
                        if column>100:raise ValueError('最多接收100列。')
                        while len(cells)<column:cells.append('')
                        value=cell.findtext('s:v','',NS);kind=cell.attrib.get('t')
                        if kind=='s':value=strings[int(value)]
                        elif kind=='inlineStr':value=''.join(cell.find('s:is',NS).itertext())
                        elif kind=='e':raise ValueError('Excel包含错误单元格。')
                        elif value and int(cell.attrib.get('s',0)) in date_styles:value=(epoch+timedelta(days=float(value))).isoformat(sep=' ')
                        cells[column-1]=value
                    rows.append(cells)
        except (zipfile.BadZipFile,KeyError,ET.ParseError,IndexError,OverflowError) as error:
            raise ValueError('Excel文件结构不完整或无法读取。') from None
    else:raise ValueError('支持.csv和.xlsx；旧版.xls请在Excel另存为.xlsx。')
    if len(rows)-header_row>MAX_ROWS:raise ValueError(f'单次最多{MAX_ROWS}行，请按日期拆分报表。')
    if len(rows)<header_row:raise ValueError('文件没有指定的表头行。')
    headers=[str(v).strip() for v in rows[header_row-1]]
    while headers and not headers[-1]:headers.pop()
    if not headers or len(headers)>100 or any(not h or len(h)>100 for h in headers) or len(set(headers))!=len(headers):raise ValueError('首行需为非空且不重复的表头（最多100列、每列100字）。')
    data=[]
    for n,row in enumerate(rows[header_row:],header_row+1):
        if not any(str(v).strip() for v in row):continue
        if len(row)>len(headers) and any(row[len(headers):]):raise ValueError(f'第{n}行列数超过表头。')
        values=[str(v).strip() for v in row]+['']*max(0,len(headers)-len(row))
        if any(len(v)>4000 for v in values):raise ValueError(f'第{n}行存在过长内容。')
        data.append({'line':n,'values':dict(zip(headers,values))})
    if not data:raise ValueError('文件只有表头，没有数据行。')
    return {'headers':headers,'rows':data,'sheets':sheets}


def write_template(headers, example, xlsx=False):
    """下载模板只有表头与一行明确示例；xlsx使用文本单元格，避免ID/日期被自动改写。"""
    if not xlsx:
        text=io.StringIO();writer=csv.writer(text);writer.writerow(headers);writer.writerow(example)
        return ('\ufeff'+text.getvalue()).encode('utf-8')
    def cells(values,n):
        return '<row r="'+str(n)+'">'+''.join(f'<c r="{chr(65+i)}{n}" t="inlineStr" s="{1 if n==1 else 0}"><is><t>{escape(str(v))}</t></is></c>' for i,v in enumerate(values))+'</row>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="'+NS['s']+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="导入模板" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml','<styleSheet xmlns="'+NS['s']+'"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF81704C"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0"/></cellXfs></styleSheet>')
        cols=''.join(f'<col min="{i}" max="{i}" width="24" customWidth="1"/>' for i in range(1,len(headers)+1))
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="'+NS['s']+'"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols>'+cols+'</cols><sheetData>'+cells(headers,1)+cells(example,2)+'</sheetData><autoFilter ref="A1:'+chr(64+len(headers))+'2"/></worksheet>')
    return output.getvalue()

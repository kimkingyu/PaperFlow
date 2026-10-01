"""Tool contracts using engine models rather than permissive nested dictionaries."""
from __future__ import annotations
from typing import Annotated, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, create_model
from paperflow.engine.journals.models import SearchFilters, JournalRecord
from paperflow.engine.journals.recommendation_models import ResearchProfile, FitAssessment, RecommendationPreferences
from paperflow.engine.planning.models import ProjectProfile, ResearchPlan, Evidence, PlanningTask
from paperflow.engine.literature.models import ReadingCard
from paperflow.engine.literature.writing_models import WritingProfile, WritingQuery, WritingAssessment, WritingDraft
from paperflow.engine.literature.reading_loop_models import LoopBudget, WritingReview, FeedbackAssessment, FeedbackInterpretation, FeedbackRevision

Text = Annotated[str, Field(max_length=60000)]
ID = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')]
Revision = Annotated[int, Field(ge=1, le=2**63-2)]
Offset = Annotated[int, Field(ge=0, le=1000000)]
Limit = Annotated[int, Field(ge=1, le=100)]
Mode = Literal['auto', 'idea', 'manuscript']

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False, hide_input_in_errors=True)

class Table(Input):
    headers: list[Annotated[str, Field(max_length=500)]] = Field(min_length=1, max_length=30)
    rows: list[list[Annotated[str, Field(max_length=5000)]]] = Field(max_length=500)
    caption: Annotated[str, Field(max_length=1000)] = ''
    position: Literal['cursor','end'] = 'cursor'

class Section(Input):
    title: Annotated[str, Field(max_length=1000)]
    level: Annotated[int, Field(ge=1,le=3)] = 1
    paragraphs: list[Text] = Field(default_factory=list, max_length=500)
    tables: list[Table] = Field(default_factory=list, max_length=50)

# Partial task update keeps each existing field's type and constraints.
TaskUpdates = create_model('TaskUpdates', __base__=Input, **{
    name: (Optional[Annotated.__class_getitem__((field.annotation, *field.metadata)) if field.metadata else field.annotation], None)
    for name, field in PlanningTask.model_fields.items() if name != 'id'
})

class Reference(Input):
    title: str = Field(default='',max_length=2000)
    journal: str = Field(default='',max_length=1000)
    venue: str = Field(default='',max_length=1000)
    issn: str = Field(default='',max_length=50)
    doi: str = Field(default='',max_length=500)
    year: Optional[Annotated[int, Field(ge=1500,le=2200)]] = None


def contracts():
    result = {}
    def add(name, **fields):
        result[name] = create_model('Tool_' + name, __base__=Input, **fields)
    add('overview')
    add('search', query=(str,''), filters=(Optional[SearchFilters],None), sort_by=(str,'relevance'), limit=(Limit,20), offset=(Offset,0))
    add('details', query=(str,...))
    add('check', query=(str,...), profile_id=(Optional[ID],None), warning_years=(Optional[list[int]],None))
    add('compare', journal_ids=(Annotated[list[ID],Field(min_length=1,max_length=10)],...), rank_system=(Optional[Literal['cas','jcr','xr','ccf','ccft']],None), rank_year=(Optional[int],None))
    add('prepare', text=(Text,''), asset_id=(Optional[ID],None), mode=(Mode,'auto'))
    add('read_project', asset_id=(ID,...), mode=(Mode,'auto'))
    add('recommend', text=(Text,''), asset_id=(Optional[ID],None), mode=(Mode,'auto'), profile=(Optional[ResearchProfile],None), assessments=(Optional[Annotated[list[FitAssessment],Field(max_length=200)]],None), candidate_records=(Optional[Annotated[list[JournalRecord],Field(max_length=200)]],None), preferences=(Optional[RecommendationPreferences],None))
    add('papers_search', query=(Annotated[str,Field(min_length=1,max_length=1000)],...), limit=(Annotated[int,Field(ge=1,le=50)],10), sources=(Optional[list[Literal['crossref','arxiv']]],None), year_from=(Optional[int],None), year_to=(Optional[int],None), sort_by=(str,'relevance'))
    for name in ('papers_details','papers_download'):
        add(name,paper_id=(ID,...))
    add('papers_read', paper_id=(ID,...),page_number=(Annotated[int,Field(ge=1,le=1000)],1),page_count=(Annotated[int,Field(ge=1,le=10)],3),offset=(Annotated[int,Field(ge=0,le=4*1024*1024)],0),max_chars=(Annotated[int,Field(ge=1000,le=20000)],20000))
    add('papers_notes', paper_id=(ID,...), reading=(ReadingCard,...))
    add('papers_import',asset_id=(ID,...),title=(Annotated[str,Field(max_length=1000)],''))
    for name in ('papers_list','writing_list','planning_list'):
        add(name,limit=(Limit,20),offset=(Offset,0),discover=(bool,False))
    add('writing_prepare',text=(Text,...))
    add('writing_create',profile=(WritingProfile,...),queries=(Optional[Annotated[list[WritingQuery],Field(max_length=6)]],None),paper_ids=(Optional[Annotated[list[ID],Field(max_length=30)]],None),source_text=(Text,''),input_id=(str,''))
    add('writing_search',project_id=(ID,...),expected_revision=(Revision,...),queries=(Optional[Annotated[list[WritingQuery],Field(min_length=1,max_length=6)]],None),per_query_limit=(Annotated[int,Field(ge=1,le=10)],10))
    add('writing_assess',project_id=(ID,...),expected_revision=(Revision,...),assessments=(Annotated[list[WritingAssessment],Field(max_length=60)],...),selected_paper_ids=(Optional[Annotated[list[ID],Field(max_length=30)]],None))
    add('writing_get',project_id=(ID,...),revision=(Optional[Revision],None),evidence_offset=(Offset,0),evidence_limit=(Annotated[int,Field(ge=1,le=100)],40))
    add('writing_materials',project_id=(ID,...),evidence_offset=(Offset,0),evidence_limit=(Annotated[int,Field(ge=1,le=100)],30))
    add('writing_draft',project_id=(ID,...),draft=(WritingDraft,...),expected_revision=(Revision,...),change_note=(Annotated[str,Field(min_length=1,max_length=1000)],'更新文献支持草稿'))
    for name in ('writing_export','planning_export','planning_get'):
        add(name,project_id=(ID,...),revision=(Optional[Revision],None))
    add('loop_get',project_id=(ID,...))
    dual = {'project_id':(ID,...),'expected_loop_revision':(Annotated[int,Field(ge=0,le=2**63-2)],...), 'expected_project_revision':(Revision,...)}
    add('loop_control',**dual, action=(Literal['start','pause','resume','stop','update_budget'],...),budget=(Optional[LoopBudget],None),request=(Optional[Annotated[str,Field(max_length=4000)]],None))
    add('loop_prepare_review',project_id=(ID,...),evidence_offset=(Offset,0),evidence_limit=(Limit,30))
    add('loop_submit_review',**dual,review=(WritingReview,...),context_fingerprint=(Annotated[str,Field(min_length=1,max_length=128)],...))
    add('loop_step',**dual,action_id=(ID,...))
    add('loop_feedback',**dual,action_id=(ID,...),feedback=(Union[FeedbackAssessment,FeedbackInterpretation,FeedbackRevision],...))
    add('planning_prepare',text=(Text,''),asset_id=(Optional[ID],None),max_chars=(Annotated[int,Field(ge=1000,le=100000)],60000))
    add('planning_create',profile=(ProjectProfile,...))
    add('planning_save',project_id=(ID,...),plan=(ResearchPlan,...),expected_revision=(Revision,...),change_note=(Annotated[str,Field(min_length=1,max_length=1000)],'更新研究规划'))
    add('planning_task',project_id=(ID,...),task_id=(ID,...),updates=(TaskUpdates,...),expected_revision=(Revision,...))
    add('planning_evidence',project_id=(ID,...),evidence=(Evidence,...),expected_revision=(Revision,...),experiment_ids=(Optional[Annotated[list[ID],Field(max_length=200)]],None))
    add('journal_sources',source_id=(str,''))
    add('journal_build',dry_run=(bool,True),force=(bool,False),asset_ids=(Optional[Annotated[list[ID],Field(max_length=30)]],None))
    add('journal_import',source_id=(ID,...),asset_id=(ID,...),kind=(str,'auto'),data_year=(Optional[int],None),encoding=(Literal['utf-8-sig','utf-8'], 'utf-8-sig'),dry_run=(bool,True),source_version=(str,''),dataset_id=(str,''),allow_shrink=(bool,False))
    add('journal_refresh',source_id=(ID,...),dataset_id=(str,...),dry_run=(bool,True))
    add('journal_peers',references=(Optional[Annotated[list[Reference],Field(max_length=200)]],None),asset_id=(Optional[ID],None),filters=(Optional[SearchFilters],None))
    # Provider JSON has variable event keys: accept text, parse with the actual offline parser.
    add('submission_track',json_text=(Text,''),asset_id=(Optional[ID],None),previous_asset_id=(Optional[ID],None),provider=(Literal['elsevier'],'elsevier'),include_title=(bool,False))
    add('documents_standards',category=(Literal['all','reference_gb','structure_gb','layout_preset','international'],'all'))
    for name in ('documents_audit','documents_normalize'):
        add(name,asset_id=(ID,...),standard_id=(str,'chinese_thesis_standard'))
    add('documents_generate',title=(Annotated[str,Field(min_length=1,max_length=1000)],...),abstract=(Text,''),keywords=(Annotated[list[str],Field(max_length=30)],[]),sections=(Annotated[list[Section],Field(max_length=100)],...),is_chinese=(bool,True))
    add('documents_language_check',text=(Text,...))
    add('word_status')
    add('word_targets')
    add('word_select',document_id=(Optional[ID],None),asset_id=(Optional[ID],None))
    add('word_selection',document_id=(ID,...))
    word = {'document_id':(ID,...),'selection_fingerprint':(Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')],...)}
    add('word_insert',**word,content=(Text,...),heading_level=(Annotated[int,Field(ge=-1,le=3)],0),position=(Literal['cursor','end'],'cursor'))
    add('word_replace',**word,new_text=(Text,...))
    add('word_comment',**word,comment_text=(Annotated[str,Field(min_length=1,max_length=4000)],...),author=(Annotated[str,Field(max_length=300)],'PaperFlow AI'))
    add('word_track',**word,enable=(bool,True))
    add('word_table',**word,headers=(Table.model_fields['headers'].annotation,...),rows=(Table.model_fields['rows'].annotation,...),caption=(str,''),position=(Literal['cursor','end'],'cursor'))
    add('word_style',**word,standard_id=(Literal['chinese_thesis_standard','chinese_journal_standard','ieee_conference_2024','apa_7th_edition'],'chinese_thesis_standard'))
    return result

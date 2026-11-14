"""Identity, output modality and reasoning-capability regression tests."""
from __future__ import annotations
import os, tempfile
from pathlib import Path
from dataclasses import replace
import pytest
from kiro_byo.config import ModelSpec, ProviderSpec, KiroByoConfig, _load_model, _load_provider
from kiro_byo.mode_router import route_model
from kiro_byo.policy_packs import get_policy_pack
from kiro_byo.reasoning_control import (ReasoningInvocation, thinking_auto_suggestion,
       resolve_thinking_mode,resolve_effort_level)
from kiro_byo.provider_catalog import merge_official_model_profile

FORMS=[('tiga','sky'),('tiga','multi'),('tiga','power'),('tiga','glitter'),
       ('dyna','miracle'),('dyna','flash'),('dyna','strong')]

def mk_model(**kw):
    return ModelSpec('test','Test',kw.pop('model_id','m'),kw.pop('name','M'),**kw)

def resolve(pack,form,model,complexity):
    profile=get_policy_pack(pack).policy(form).auto_reasoning
    context=ReasoningInvocation(complexity=complexity,code_task=complexity>.5,role='lead',stage='planning')
    suggestion=thinking_auto_suggestion(profile.thinking, context)
    thought=resolve_thinking_mode(model,model.thinking_preference,suggestion)
    return thought, resolve_effort_level(model,strategy=profile.effort,context=context,effective_thinking=thought)

@pytest.mark.parametrize('pack,form',FORMS)
@pytest.mark.parametrize('pref',('on','off'))
def test_switchable_user_lock_controls_every_form(pack,form,pref):
    m=mk_model(thinking_preference=pref,reasoning_supported=True,reasoning_can_disable=True,
               effort_supported_levels=('low','high','max'))
    thinking,effort=resolve(pack,form,m,.91)
    assert thinking==pref
    assert (not effort) if pref=='off' else effort in m.effort_supported_levels

@pytest.mark.parametrize('pack,form',FORMS)
def test_fixed_on_models_cannot_be_disabled_by_any_form(pack,form):
    m=mk_model(thinking_preference='off',reasoning_supported=True,reasoning_can_disable=False,
               effort_supported_levels=())
    assert resolve(pack,form,m,.02)==('on','')

@pytest.mark.parametrize('pack,form',FORMS)
def test_no_reasoning_models_cannot_be_forced_on_by_any_form(pack,form):
    m=mk_model(thinking_preference='on',reasoning_supported=False,reasoning_can_disable=False,
               effort_supported_levels=())
    assert resolve(pack,form,m,.99)==('off','')

@pytest.mark.parametrize('pack,form',FORMS)
def test_fixed_on_no_effort_always_omits_effort(pack,form):
    m=mk_model(thinking_preference='auto',reasoning_supported=True,reasoning_can_disable=False,
               effort_supported_levels=())
    assert resolve(pack,form,m,.99)==('on','')

@pytest.mark.parametrize('pack,form',FORMS)
def test_auto_effort_always_from_model_vocabulary(pack,form):
    m=mk_model(thinking_preference='auto',reasoning_supported=True,reasoning_can_disable=True,
               effort_supported_levels=('low','superhigh'))
    for complexity in (.01,.75,.99):
        thinking, effort=resolve(pack,form,m,complexity)
        assert not effort or effort in ('low','superhigh')
        assert thinking!='off' or not effort

def test_relay_does_not_trust_official_alias_for_capabilities():
    raw={'id':'pretend-opus-5-5','name':'DeepSeek-V4.1-Flash'}
    p=_load_provider({'id':'relay','preset_id':'deepseek','type':'openai-compatible',
       'base_url':'https://relay.invalid/v1','models':[raw]},resolve_credentials=False)
    assert p.models[0].effort_supported_levels==()
    assert p.type=='openai-compatible'

def test_missing_reasoning_status_with_effort_levels_is_normalized_in_saved_config():
    m=_load_model('p','P',{'id':'m','effort_supported_levels':['low','high']})
    assert m.reasoning_supported is True

def test_official_alias_conflict_must_not_certify_unrelated_model():
    prof=merge_official_model_profile('deepseek',{'id':'pretend-opus-5-5','name':'DeepSeek-V4.1-Flash'})
    assert not prof.get('effort_supported_levels')

def test_chat_router_must_exclude_video_only_output():
    good=mk_model(output_modalities=('text',),routing_tiers=('fast','balanced','strong'),routing_quality=.5,routing_speed=.5,routing_economy=.5)
    bad=replace(good,model_id='video',output_modalities=('video',),routing_quality=1.,routing_speed=1.,routing_economy=1.)
    cfg=KiroByoConfig((ProviderSpec('test','Test','openai-compatible','https://invalid.example/v1','',(good,bad)),))
    d=route_model(cfg,pack='tiga',policy='sky',prompt='Hi',history=[],has_image=False,tools_required=False,tools_available=True)
    assert d and d.model.model_id=='m'

def test_effort_must_not_be_sent_when_thinking_support_unknown():
    m=mk_model(reasoning_supported=None,reasoning_can_disable=None,effort_supported_levels=('low','high'))
    thinking,effort=resolve('tiga','power',m,.99)
    assert thinking=='' and effort==''

@pytest.mark.parametrize('pack,form',FORMS)
def test_type_never_advertises_its_chosen_models_effort_as_session_wide_acp(pack,form):
    from kiro_byo.acp_server import AcpServer, Session
    from kiro_byo.protocol import EFFORT_CONFIG_ID, type_selection_id
    m=mk_model(reasoning_supported=True, reasoning_can_disable=True,
        effort_supported_levels=('low','high'),reasoning_enable_value='enabled',reasoning_disable_value='none')
    cfg=KiroByoConfig((ProviderSpec('test','Test','openai-compatible','https://invalid.example/v1','',(m,)),))
    options=AcpServer._config_options(Session('s','.',type_selection_id(pack,form)),cfg)
    assert EFFORT_CONFIG_ID not in [row['id'] for row in options]

def test_fixed_concrete_model_only_advertises_real_supported_effort_levels():
    from kiro_byo.acp_server import AcpServer, Session
    from kiro_byo.protocol import EFFORT_CONFIG_ID
    m=mk_model(reasoning_supported=True,reasoning_can_disable=True,effort_supported_levels=('low','high'),
        reasoning_enable_value='enabled',reasoning_disable_value='none')
    cfg=KiroByoConfig((ProviderSpec('test','Test','openai-compatible','https://invalid.example/v1','',(m,)),))
    opt=AcpServer._config_options(Session('s','.','M'),cfg)
    effort=next(row for row in opt if row['id']==EFFORT_CONFIG_ID)
    assert [row['value'] for row in effort['options']]==['low','high']

def test_fixed_model_with_no_selectable_effort_omits_acp_effort_option():
    from kiro_byo.acp_server import AcpServer, Session
    from kiro_byo.protocol import EFFORT_CONFIG_ID
    for levels in [(),('high',)]:
        m=mk_model(reasoning_supported=True,reasoning_can_disable=False,effort_supported_levels=levels)
        cfg=KiroByoConfig((ProviderSpec('test','Test','openai-compatible','https://invalid.example/v1','',(m,)),))
        assert EFFORT_CONFIG_ID not in [x['id'] for x in AcpServer._config_options(Session('s','.','M'),cfg)]


@pytest.mark.parametrize('pack,form', FORMS)
@pytest.mark.parametrize('output', [('image',), ('video',), ('audio',)])
def test_text_acp_route_never_selects_generation_only_model(pack,form,output):
    good=mk_model(output_modalities=('text',),routing_tiers=('fast','balanced','strong'),
                  routing_quality=.2,routing_speed=.2,routing_economy=.2)
    bad=replace(good,model_id='generator',output_modalities=output,
                routing_quality=1.0,routing_speed=1.0,routing_economy=1.0)
    cfg=KiroByoConfig((ProviderSpec('test','Test','openai-compatible',
                                   'https://invalid.example/v1','',(bad,good)),))
    decision=route_model(cfg,pack=pack,policy=form,prompt='Hello',history=[],has_image=False,
                         tools_required=False,tools_available=True)
    assert decision and decision.model.model_id=='m'
    assert any(row[1]=='generator' and 'text output' in row[2] for row in decision.exclusions)


def test_alias_is_not_performance_evidence():
    from kiro_byo.mode_router import _model_priors, model_tiers
    a=mk_model(name='DeepSeek Flash',model_id='fake-opus-max')
    b=mk_model(name='Opus Pro',model_id='fake-deepseek-flash')
    assert _model_priors(a)[:3]==_model_priors(b)[:3]
    assert model_tiers(a,learned_lookup=lambda *_: '') == ('balanced',)
    assert model_tiers(b,learned_lookup=lambda *_: '') == ('balanced',)


@pytest.mark.parametrize('pack,form', FORMS)
@pytest.mark.parametrize('tool,image', [(True,False),(False,True),(True,True)])
def test_declared_capabilities_win_over_unknown_aliases(pack,form,tool,image):
    good=mk_model(model_id='verified',input_modalities=('text','image'),tool_use=True,
                  output_modalities=('text',),routing_tiers=('balanced','fast','strong'),
                  routing_quality=.2,routing_speed=.2,routing_economy=.2)
    unknown=replace(good,model_id='fake-flash',name='Some Fast Model',input_modalities=(),tool_use=None,
                    routing_quality=1.,routing_speed=1.,routing_economy=1.)
    cfg=KiroByoConfig((ProviderSpec('test','Test','openai-compatible',
                                   'https://invalid.example/v1','',(good,unknown)),))
    decision=route_model(cfg,pack=pack,policy=form,prompt='Please inspect image and tool',history=[],
                         has_image=image,tools_required=tool,tools_available=True)
    assert decision and decision.model.model_id=='verified'


def test_effort_unknown_is_absent_at_transport_and_acp_layer():
    from kiro_byo.reasoning_control import transport_effort_level,review_effort_level
    from kiro_byo.acp_server import AcpServer, Session
    from kiro_byo.protocol import EFFORT_CONFIG_ID
    m=mk_model(reasoning_supported=None,effort_supported_levels=('low','high'))
    assert m.selectable_effort_levels==()
    assert transport_effort_level(m,'on','high')==''
    assert review_effort_level(m,'high',True,'high')==''
    cfg=KiroByoConfig((ProviderSpec('test','Test','openai-compatible',
        'https://invalid.example/v1','',(m,)),))
    assert EFFORT_CONFIG_ID not in [r['id'] for r in AcpServer._config_options(Session('s','.','M'),cfg)]

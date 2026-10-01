export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Data = Record<string, any>;
export interface Schema {
  type?: string; title?: string; description?: string; default?: any; enum?: any[]; const?: any;
  properties?: Record<string, Schema>; required?: string[]; items?: Schema | Schema[];
  anyOf?: Schema[]; oneOf?: Schema[]; $ref?: string; $defs?: Record<string, Schema>;
  minimum?: number; maximum?: number; minLength?: number; maxLength?: number;
  minItems?: number; maxItems?: number; pattern?: string;
}
export interface Tool { name: string; description: string; input_schema: Schema; category: string; mutates: boolean; network: boolean; desktop: boolean; approval_required: boolean; available: boolean; unavailable_reason?: string; }
export interface Workspace { id: string; title: string; description: string; links: { kind: string; target_id: string }[]; created_at: string; updated_at: string; }
export interface Artifact { id?: string; artifact_id?: string; filename?: string; title?: string; media_type?: string; size?: number; created_at?: string; source?: string; metadata?: { source_action?: string; source_tool?: string; origin?: string; project_id?: string | null; project_revision?: number | null; run_id?: string | null; input_asset_refs?: { asset_id: string; sha256?: string }[] }; }
export interface Run { id: string; session_id: string; workspace_id?: string; status: string; usage?: Data; budget?: Data; pending_approval?: { approval_id: string; tool_name: string; arguments: Data; reason: string }; error?: any; }
export interface Session { id: string; title: string; workspace_id?: string; messages?: Data[]; runs?: Run[]; }
export interface RunEvent { seq: number; type: string; data: Data; }

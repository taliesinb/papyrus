import { brand } from '#lib/server/db.ts';
import { pipelineStatus } from '#lib/server/detail.ts';
import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = () => ({ status: pipelineStatus(), brand: brand() });

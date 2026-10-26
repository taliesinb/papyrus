import { pipelineStatus } from '#lib/server/detail.ts';
import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = () => ({ status: pipelineStatus() });

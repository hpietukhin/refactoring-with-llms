package planning.ast;

import com.fasterxml.jackson.databind.ObjectMapper;
import spoon.Launcher;
import spoon.reflect.CtModel;
import spoon.reflect.code.CtConstructorCall;
import spoon.reflect.code.CtInvocation;
import spoon.reflect.cu.SourcePosition;
import spoon.reflect.declaration.CtClass;
import spoon.reflect.declaration.CtConstructor;
import spoon.reflect.declaration.CtElement;
import spoon.reflect.declaration.CtEnum;
import spoon.reflect.declaration.CtExecutable;
import spoon.reflect.declaration.CtInterface;
import spoon.reflect.declaration.CtMethod;
import spoon.reflect.declaration.CtType;
import spoon.reflect.reference.CtExecutableReference;
import spoon.reflect.visitor.filter.TypeFilter;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

public final class SpoonAstIndexer {
    private final Path repository;
    private final List<ElementData> elements = new ArrayList<>();
    private final Set<EdgeData> edges = new HashSet<>();
    private final Map<CtElement, String> idsByElement = new IdentityHashMap<>();
    private final Set<String> projectElementIds = new HashSet<>();
    private int unresolvedCalls;

    private SpoonAstIndexer(Path repository) {
        this.repository = repository.toAbsolutePath().normalize();
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException(
                    "Usage: java -jar spoon-ast-indexer.jar REPOSITORY"
            );
        }
        SpoonAstIndexer indexer = new SpoonAstIndexer(Path.of(args[0]));
        System.out.println(
                new ObjectMapper().writeValueAsString(indexer.build())
        );
    }

    private Output build() throws IOException {
        Launcher launcher = new Launcher();
        launcher.getEnvironment().setNoClasspath(true);
        launcher.getEnvironment().setIgnoreSyntaxErrors(true);
        launcher.getEnvironment().setCommentEnabled(false);
        launcher.getEnvironment().setComplianceLevel(25);
        for (Path root : sourceRoots()) {
            launcher.addInputResource(root.toString());
        }
        launcher.buildModel();
        CtModel model = launcher.getModel();

        registerElements(model);
        registerContainment();
        registerCalls(model);

        elements.sort(
                Comparator.comparing(ElementData::file)
                        .thenComparingInt(ElementData::startLine)
                        .thenComparing(ElementData::id)
        );
        List<EdgeData> sortedEdges = edges.stream()
                .sorted(
                        Comparator.comparing(EdgeData::source)
                                .thenComparing(EdgeData::target)
                                .thenComparing(EdgeData::relation)
                )
                .toList();
        return new Output(elements, sortedEdges, unresolvedCalls);
    }

    private void registerElements(CtModel model) {
        for (CtType<?> type : model.getAllTypes()) {
            register(type, type.getQualifiedName(), typeKind(type));
        }
        for (CtExecutable<?> executable : model.getElements(
                new TypeFilter<>(CtExecutable.class)
        )) {
            if (!(executable instanceof CtMethod<?>)
                    && !(executable instanceof CtConstructor<?>)) {
                continue;
            }
            register(
                    executable,
                    executableId(executable.getReference()),
                    executableKind(executable)
            );
        }
    }

    private void register(CtElement element, String id, String kind) {
        SourcePosition position = element.getPosition();
        if (!position.isValidPosition() || position.getFile() == null) {
            return;
        }
        Path file = position.getFile().toPath().toAbsolutePath().normalize();
        if (!file.startsWith(repository)) {
            return;
        }
        String relative = repository.relativize(file)
                .toString()
                .replace('\\', '/');
        idsByElement.put(element, id);
        projectElementIds.add(id);
        elements.add(
                new ElementData(
                        id,
                        kind,
                        relative,
                        position.getLine(),
                        position.getEndLine()
                )
        );
    }

    private void registerContainment() {
        for (Map.Entry<CtElement, String> entry : idsByElement.entrySet()) {
            nearestElement(entry.getKey().getParent(), entry.getKey())
                    .ifPresent(parentId -> edges.add(
                            new EdgeData(parentId, entry.getValue(), "contains")
                    ));
        }
    }

    private void registerCalls(CtModel model) {
        for (CtInvocation<?> invocation : model.getElements(
                new TypeFilter<>(CtInvocation.class)
        )) {
            registerCall(
                    invocation,
                    invocation.getExecutable().getDeclaration()
            );
        }
        for (CtConstructorCall<?> constructorCall : model.getElements(
                new TypeFilter<>(CtConstructorCall.class)
        )) {
            registerCall(
                    constructorCall,
                    constructorCall.getExecutable().getDeclaration()
            );
        }
    }

    private void registerCall(CtElement call, CtExecutable<?> declaration) {
        java.util.Optional<String> source = nearestElement(
                call.getParent(),
                null
        );
        if (source.isEmpty() || declaration == null) {
            unresolvedCalls++;
            return;
        }
        String target = idsByElement.get(declaration);
        if (target == null) {
            target = executableId(declaration.getReference());
        }
        if (projectElementIds.contains(target)) {
            edges.add(new EdgeData(source.get(), target, "calls"));
        }
    }

    private java.util.Optional<String> nearestElement(
            CtElement start,
            CtElement excluded
    ) {
        CtElement current = start;
        while (current != null && !current.isParentInitialized()) {
            current = null;
        }
        while (current != null) {
            if (current != excluded && idsByElement.containsKey(current)) {
                return java.util.Optional.of(idsByElement.get(current));
            }
            if (!current.isParentInitialized()) {
                break;
            }
            current = current.getParent();
        }
        return java.util.Optional.empty();
    }

    private String typeKind(CtType<?> type) {
        if (type instanceof CtInterface<?>) {
            return "interface";
        }
        if (type instanceof CtEnum<?>) {
            return "enum";
        }
        if (type instanceof CtClass<?>) {
            return "class";
        }
        return "type";
    }

    private String executableKind(CtExecutable<?> executable) {
        return executable instanceof CtMethod<?> ? "method" : "constructor";
    }

    private String executableId(CtExecutableReference<?> reference) {
        String owner = reference.getDeclaringType() == null
                ? "<unknown>"
                : reference.getDeclaringType().getQualifiedName();
        return owner + "#" + reference.getSignature();
    }

    private Set<Path> sourceRoots() throws IOException {
        Set<Path> roots = new LinkedHashSet<>();
        try (Stream<Path> paths = Files.walk(repository)) {
            for (Path directory : paths.filter(Files::isDirectory).toList()) {
                String normalized = directory.toString().replace('\\', '/');
                if (normalized.endsWith("/src/main/java")
                        || normalized.endsWith("/src/test/java")) {
                    roots.add(directory);
                }
            }
        }
        if (roots.isEmpty()) {
            roots.add(repository);
        }
        return roots;
    }

    private record ElementData(
            String id,
            String kind,
            String file,
            int startLine,
            int endLine
    ) {
    }

    private record EdgeData(String source, String target, String relation) {
    }

    private record Output(
            List<ElementData> elements,
            List<EdgeData> edges,
            int unresolvedCalls
    ) {
    }
}
